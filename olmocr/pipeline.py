import argparse
import asyncio
import atexit
import base64
import datetime
import hashlib
import json
import logging
import multiprocessing
import os
import random
import re
import shutil
import sys
import tempfile
import time
import glob
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from functools import cache, partial
from io import BytesIO
from urllib.parse import urlparse

import boto3
import httpx
import torch
from botocore.exceptions import ClientError
from huggingface_hub import snapshot_download
from PIL import Image
from pypdf import PdfReader
from tqdm import tqdm

from dotenv import load_dotenv

from olmocr.check import (
    check_poppler_version,
    check_sglang_version,
    check_torch_gpu_available,
)
from olmocr.data.renderpdf import render_pdf_to_base64png
from olmocr.filter.filter import Language, PdfFilter
from olmocr.image_utils import convert_image_to_pdf_bytes, is_jpeg, is_png
from olmocr.metrics import MetricsKeeper, WorkerTracker
from olmocr.prompts import PageResponse, build_finetuning_prompt
from olmocr.prompts.anchor import get_anchor_text
from olmocr.s3_utils import (
    download_directory,
    download_zstd_csv,
    expand_s3_glob,
    get_s3_bytes,
    get_s3_bytes_with_backoff,
    parse_s3_path,
)
from olmocr.version import VERSION
from olmocr.work_queue import LocalWorkQueue, S3WorkQueue, WorkQueue

from pathlib import Path
from tabulate import tabulate  # 需要安装：pip install tabulate

# Initialize logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.propagate = False

sglang_logger = logging.getLogger("sglang")
sglang_logger.propagate = False

file_handler = logging.FileHandler("olmocr-pipeline-debug.log", mode="a")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))

# Add handlers to the logger
logger.addHandler(file_handler)
logger.addHandler(console_handler)
sglang_logger.addHandler(file_handler)

# Quiet logs from pypdf
logging.getLogger("pypdf").setLevel(logging.ERROR)

# Global s3 clients fo the whole script, we have two separate ones in case your workspace and your pdfs are in different accounts
workspace_s3 = boto3.client("s3")
pdf_s3 = boto3.client("s3")

# Global variables for token statistics
metrics = MetricsKeeper(window=60 * 5)
tracker = WorkerTracker()

# Process pool for offloading cpu bound work, like calculating anchor texts, max 32 workers, otherwise it can spawn way too many workers on a big machine
process_pool = ProcessPoolExecutor(max_workers=min(multiprocessing.cpu_count() // 2 + 1, 32), mp_context=multiprocessing.get_context("spawn"))

# Filter object, cached so it will only get loaded when/if you need it
get_pdf_filter = cache(lambda: PdfFilter(languages_to_keep={Language.ENGLISH, None}, apply_download_spam_check=True, apply_form_check=True))

# Specify a default port, but it can be overridden by args
SGLANG_SERVER_PORT = 30024


@dataclass(frozen=True)
class PageResult:
    s3_path: str
    page_num: int
    response: PageResponse

    input_tokens: int
    output_tokens: int
    is_fallback: bool

# 修改整个函数 从本地调用改成API调用
import os

async def build_page_query(local_pdf_path: str, page: int, target_longest_image_dim: int, target_anchor_text_len: int, image_rotation: int = 0) -> dict:
    MAX_TOKENS = 3000
    assert image_rotation in [0, 90, 180, 270], "Invalid image rotation provided in build_page_query"

    # 1. 渲染图片和获取锚文本
    image_base64 = asyncio.to_thread(render_pdf_to_base64png, local_pdf_path, page, target_longest_image_dim=target_longest_image_dim)
    loop = asyncio.get_running_loop()
    anchor_text = loop.run_in_executor(
        process_pool, partial(get_anchor_text, pdf_engine="pdfreport", target_length=target_anchor_text_len), local_pdf_path, page
    )
    image_base64, anchor_text = await asyncio.gather(image_base64, anchor_text)  # type: ignore

    if image_rotation != 0:
        image_bytes = base64.b64decode(image_base64)
        with Image.open(BytesIO(image_bytes)) as img:
            rotated_img = img.rotate(-image_rotation, expand=True)
            buffered = BytesIO()
            rotated_img.save(buffered, format="PNG")
        image_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

    # 2. 通过环境变量读取 model
    model_name = os.getenv("OPENAI_API_MODEL", "gpt-4.1")

    # 3. 构造 vision 格式的消息
    return {
        "model": model_name,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}},
                    {"type": "text", "text": build_finetuning_prompt(anchor_text)},
                ],
            }
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.0,
    }


# Manual simple implementation of HTTP Post
# It feels strange perhaps, but httpx and aiohttp are very complex beasts
# Ex. the sessionpool in httpcore has 4 different locks in it, and I've noticed
# that at the scale of 100M+ requests, that they deadlock in different strange ways
async def apost(url, json_data):
    parsed_url = urlparse(url)
    host = parsed_url.hostname
    port = parsed_url.port or 80
    path = parsed_url.path or "/"

    writer = None
    try:
        reader, writer = await asyncio.open_connection(host, port)

        json_payload = json.dumps(json_data)
        request = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(json_payload)}\r\n"
            f"Connection: close\r\n\r\n"
            f"{json_payload}"
        )
        writer.write(request.encode())
        await writer.drain()

        # Read status line
        status_line = await reader.readline()
        if not status_line:
            raise ConnectionError("No response from server")
        status_parts = status_line.decode().strip().split(" ", 2)
        if len(status_parts) < 2:
            raise ValueError(f"Malformed status line: {status_line.decode().strip()}")
        status_code = int(status_parts[1])

        # Read headers
        headers = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            key, _, value = line.decode().partition(":")
            headers[key.strip().lower()] = value.strip()

        # Read response body
        if "content-length" in headers:
            body_length = int(headers["content-length"])
            response_body = await reader.readexactly(body_length)
        else:
            raise ConnectionError("Anything other than fixed content length responses are not implemented yet")

        return status_code, response_body
    except Exception as e:
        # Pass through errors
        raise e
    finally:
        # But just make sure to close the socket on your way out
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass


async def process_page(args, worker_id: int, pdf_orig_path: str, pdf_local_path: str, page_num: int) -> PageResult:
    COMPLETION_URL = f"http://localhost:{SGLANG_SERVER_PORT}/v1/chat/completions"
    
    import os

    # 新增：从环境变量读取API参数
    REMOTE_API_BASE = os.getenv("OPENAI_API_BASE", "").rstrip("/")
    REMOTE_API_PATH = os.getenv("OPENAI_API_PATH", "").lstrip("/")
    REMOTE_API_KEY = os.getenv("OPENAI_API_KEY")
    REMOTE_COMPLETION_URL = f"{REMOTE_API_BASE}/{REMOTE_API_PATH}"
    
    MAX_RETRIES = args.max_page_retries
    TEMPERATURE_BY_ATTEMPT = [0.1, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    exponential_backoffs = 0
    local_anchor_text_len = args.target_anchor_text_len
    local_image_rotation = 0
    attempt = 0
    await tracker.track_work(worker_id, f"{pdf_orig_path}-{page_num}", "started")

    while attempt < MAX_RETRIES:
        query = await build_page_query(pdf_local_path, page_num, args.target_longest_image_dim, local_anchor_text_len, image_rotation=local_image_rotation)
        query["temperature"] = TEMPERATURE_BY_ATTEMPT[
            min(attempt, len(TEMPERATURE_BY_ATTEMPT) - 1)
        ]
        logger.info(f"Built page query for {pdf_orig_path}-{page_num}")

        try:
            if getattr(args, "use_remote_api", False):
                headers = {
                    "Authorization": f"Bearer {REMOTE_API_KEY}",
                    "Content-Type": "application/json"
                }
                async with httpx.AsyncClient(timeout=120.0) as client: # 新增：120秒超时
                    response = await client.post(REMOTE_COMPLETION_URL, headers=headers, json=query)
                    # logger.info(f"Debug--API response status: {response.status_code}, body: {response.text}") # body日志
         
                    status_code = response.status_code
                    response_body = response.content
            else:
                status_code, response_body = await apost(COMPLETION_URL, json_data=query)

            # 后续处理（无论哪种分支都要有）
            if status_code == 400:
                raise ValueError(f"Got BadRequestError from server: {response_body}, skipping this response")
            elif status_code == 500:
                raise ValueError(f"Got InternalServerError from server: {response_body}, skipping this response")
            elif status_code != 200:
                raise ValueError(f"Error http status {status_code}")

            base_response_data = json.loads(response_body)

            if base_response_data["usage"]["total_tokens"] > args.model_max_context:
                local_anchor_text_len = max(1, local_anchor_text_len // 2)
                logger.info(f"Reducing anchor text len to {local_anchor_text_len} for {pdf_orig_path}-{page_num}")
                raise ValueError("Response exceeded model_max_context, cannot use this response")

            metrics.add_metrics(
                sglang_input_tokens=base_response_data["usage"].get("prompt_tokens", 0),
                sglang_output_tokens=base_response_data["usage"].get("completion_tokens", 0),
            )
            content = base_response_data["choices"][0]["message"]["content"]
            
            try:
                model_response_json = json.loads(content)
            except json.JSONDecodeError:
                # 兼容API返回纯文本
                model_response_json = {
                    "natural_text": content,
                    "primary_language": None,
                    "is_rotation_valid": True,
                    "rotation_correction": 0,
                    "is_table": False,
                    "is_diagram": False
                }
            page_response = PageResponse(**model_response_json)

            if not page_response.is_rotation_valid and attempt < MAX_RETRIES - 1:
                logger.info(
                    f"Got invalid_page rotation for {pdf_orig_path}-{page_num} attempt {attempt}, retrying with {page_response.rotation_correction} rotation"
                )
                local_image_rotation = page_response.rotation_correction
                raise ValueError(f"invalid_page rotation for {pdf_orig_path}-{page_num}")

            await tracker.track_work(worker_id, f"{pdf_orig_path}-{page_num}", "finished")
            return PageResult(
                pdf_orig_path,
                page_num,
                page_response,
                input_tokens=base_response_data["usage"].get("prompt_tokens", 0),
                output_tokens=base_response_data["usage"].get("completion_tokens", 0),
                is_fallback=False,
            )
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            logger.warning(f"Client error on attempt {attempt} for {pdf_orig_path}-{page_num}: {type(e)} {e}")

            # Now we want to do exponential backoff, and not count this as an actual page retry
            sleep_delay = 10 * (2**exponential_backoffs)
            exponential_backoffs += 1
            logger.info(f"Sleeping for {sleep_delay} seconds on {pdf_orig_path}-{page_num} to allow server restart")
            await asyncio.sleep(sleep_delay)
        except asyncio.CancelledError:
            logger.info(f"Process page {pdf_orig_path}-{page_num} cancelled")
            await tracker.track_work(worker_id, f"{pdf_orig_path}-{page_num}", "cancelled")
            raise
        except json.JSONDecodeError as e:
            logger.warning(f"JSON decode error on attempt {attempt} for {pdf_orig_path}-{page_num}: {e}")
            attempt += 1
        except ValueError as e:
            logger.warning(f"ValueError on attempt {attempt} for {pdf_orig_path}-{page_num}: {type(e)} - {e}")
            attempt += 1
        except Exception as e:
            logger.exception(f"Unexpected error on attempt {attempt} for {pdf_orig_path}-{page_num}: {type(e)} - {e}")
            attempt += 1
    # 如果所有尝试都失败，返回一个 fallback PageResult
    return PageResult(
        pdf_orig_path,
        page_num,
        PageResponse(
            natural_text=None,
            primary_language=None,
            is_rotation_valid=True,
            rotation_correction=0,
            is_table=False,
            is_diagram=False,
        ),
        input_tokens=0,
        output_tokens=0,
        is_fallback=True,
    )

async def process_pdf(args, worker_id: int, pdf_orig_path: str):
    with tempfile.NamedTemporaryFile("wb+", suffix=".pdf") as tf:
        try:
            data = await asyncio.to_thread(lambda: get_s3_bytes_with_backoff(pdf_s3, pdf_orig_path))
            tf.write(data)
            tf.flush()
        except ClientError as ex:
            if ex.response["Error"]["Code"] == "NoSuchKey":
                logger.info(f"S3 File Not found, skipping it completely {pdf_orig_path}")
                return None
            else:
                raise

        if is_png(tf.name) or is_jpeg(tf.name):
            logger.info(f"Converting {pdf_orig_path} from image to PDF format...")
            tf.seek(0)
            tf.write(convert_image_to_pdf_bytes(tf.name))
            tf.flush()

        try:
            reader = PdfReader(tf.name)
            num_pages = reader.get_num_pages()
        except:
            logger.exception(f"Could not count number of pages for {pdf_orig_path}, aborting document")
            return None

        logger.info(f"Got {num_pages} pages to do for {pdf_orig_path} in worker {worker_id}")

        if args.apply_filter and get_pdf_filter().filter_out_pdf(tf.name):
            logger.info(f"Filtering out pdf {pdf_orig_path}")
            return None

        # List to hold the tasks for processing each page
        page_tasks = []
        page_results = []

        try:
            async with asyncio.TaskGroup() as tg:
                for page_num in range(1, num_pages + 1):
                    task = tg.create_task(process_page(args, worker_id, pdf_orig_path, tf.name, page_num))
                    page_tasks.append(task)

            # Collect the results from the entire task group, assuming no exceptions
            page_results = [task.result() for task in page_tasks]

            num_fallback_pages = sum(page_result.is_fallback for page_result in page_results)

            if num_fallback_pages / num_pages > args.max_page_error_rate:
                logger.error(
                    f"Document {pdf_orig_path} has {num_fallback_pages} fallback pages out of {num_pages} exceeding max_page_error_rate of {args.max_page_error_rate}, discarding document."
                )
                return None
            elif num_fallback_pages > 0:
                logger.warning(
                    f"Document {pdf_orig_path} processed with {num_fallback_pages} fallback pages out of {num_pages}, proceeding to build Dolma document."
                )

            return build_dolma_document(pdf_orig_path, page_results)
        except Exception as e:
            # Check for ExceptionGroup with BrokenProcessPool
            if isinstance(e, ExceptionGroup):
                broken_pool, other = e.split(BrokenProcessPool)
                if broken_pool is not None:  # Found at least one BrokenProcessPool
                    logger.critical("Encountered BrokenProcessPool, exiting process.")
                    sys.exit(1)

            logger.exception(f"Exception in process_pdf for {pdf_orig_path}: {e}")
            # You can't build a dolma doc with even 1 failed page, so just get out of here
            # However, you don't want to propagate an exception higher up and cancel the entire work_group
            return None


def build_dolma_document(pdf_orig_path, page_results):
    # Build the document text and page spans
    document_text = ""
    pdf_page_spans = []
    current_char_pos = 0

    for index, page_result in enumerate(page_results):
        if page_result.response.natural_text is not None:
            content = page_result.response.natural_text + ("\n" if index < len(page_results) - 1 else "")
        else:
            content = ""

        start_pos = current_char_pos
        document_text += content
        current_char_pos = len(document_text)
        pdf_page_spans.append([start_pos, current_char_pos, page_result.page_num])

    if not document_text:
        logger.info(f"No document text for {pdf_orig_path}")
        return None  # Return None if the document text is empty

    # Build the Dolma document
    metadata = {
        "Source-File": pdf_orig_path,
        "olmocr-version": VERSION,
        "pdf-total-pages": len(page_results),
        "total-input-tokens": sum(page.input_tokens for page in page_results),
        "total-output-tokens": sum(page.output_tokens for page in page_results),
        "total-fallback-pages": sum(page.is_fallback for page in page_results),
    }

    id_ = hashlib.sha1(document_text.encode()).hexdigest()

    dolma_doc = {
        "id": id_,
        "text": document_text,
        "source": "olmocr",
        "added": datetime.datetime.now().strftime("%Y-%m-%d"),
        "created": datetime.datetime.now().strftime("%Y-%m-%d"),
        "metadata": metadata,
        "attributes": {"pdf_page_numbers": pdf_page_spans},
    }
    return dolma_doc


@dataclass
class PdfStats:
    pdf_path: str
    page_count: int
    file_size_bytes: int
    markdown_char_count: int
    markdown_line_count: int

def collect_pdf_stats(pdf_path: str, markdown_path: str) -> PdfStats:
    """收集单个 PDF 和其对应 Markdown 的统计信息"""
    try:
        with open(pdf_path, "rb") as f:
            reader = PdfReader(f)
            page_count = len(reader.pages)
        file_size = os.path.getsize(pdf_path)
        with open(markdown_path, "r", encoding="utf-8") as f:
            markdown_content = f.read()
            markdown_char_count = len(markdown_content)
            markdown_line_count = len(markdown_content.splitlines())
        return PdfStats(
            pdf_path=pdf_path,
            page_count=page_count,
            file_size_bytes=file_size,
            markdown_char_count=markdown_char_count,
            markdown_line_count=markdown_line_count
        )
    except Exception as e:
        logger.warning(f"Failed to collect stats for {pdf_path}: {e}")
        return PdfStats(pdf_path=pdf_path, page_count=0, file_size_bytes=0, markdown_char_count=0, markdown_line_count=0)

def summarize_directory_stats(pdf_paths: list[str], markdown_paths: dict[str, str]) -> dict:
    """统计文件夹结构和每个 PDF 的信息"""
    stats = []
    folder_structure = {}
    
    for pdf_path in pdf_paths:
        pdf_dir = os.path.dirname(pdf_path)
        pdf_basename = os.path.basename(pdf_path)
        if pdf_dir not in folder_structure:
            folder_structure[pdf_dir] = []
        folder_structure[pdf_dir].append(pdf_path)
        
        markdown_path = markdown_paths.get(pdf_path, "")
        if markdown_path and os.path.exists(markdown_path):
            stat = collect_pdf_stats(pdf_path, markdown_path)
            stats.append(stat)
    
    return {
        "stats": stats,
        "folder_structure": folder_structure
    }

def print_stats_summary(stats_data: dict):
    """打印统计信息表格"""
    stats = stats_data["stats"]
    folder_structure = stats_data["folder_structure"]
    
    # 打印文件夹结构概览
    logger.info("Directory Structure Summary:")
    folder_table = []
    for folder, pdfs in folder_structure.items():
        folder_table.append([folder, len(pdfs)])
    logger.info(tabulate(folder_table, headers=["Folder Path", "PDF Count"], tablefmt="grid"))
    
    # 打印详细统计
    if stats:
        logger.info("\nDetailed PDF Statistics:")
        table_data = [
            [
                Path(stat.pdf_path).name,
                stat.page_count,
                f"{stat.file_size_bytes / 1024:.2f} KB",
                stat.markdown_char_count,
                stat.markdown_line_count
            ]
            for stat in stats
        ]
        logger.info(
            tabulate(
                table_data,
                headers=["PDF Name", "PDF Pages", "PDF Size", "MD Chars", "MD Lines"],
                tablefmt="grid"
            )
        )

async def worker(args, work_queue: WorkQueue, semaphore, worker_id):
    markdown_paths = {}  # 存储 PDF 路径到 Markdown 路径的映射
    all_pdf_paths = set()  # 存储所有处理的 PDF 路径
    
    while True:
        # Wait until allowed to proceed
        await semaphore.acquire()

        work_item = await work_queue.get_work()

        if work_item is None:
            logger.info(f"Worker {worker_id} exiting due to empty queue")
            semaphore.release()
            break

        logger.info(f"Worker {worker_id} processing work item {work_item.hash}")
        await tracker.clear_work(worker_id)
        all_pdf_paths.update(work_item.work_paths)  # 记录 PDF 路径

        try:
            async with asyncio.TaskGroup() as tg:
                dolma_tasks = [tg.create_task(process_pdf(args, worker_id, pdf)) for pdf in work_item.work_paths]
                logger.info(f"Created all tasks for {work_item.hash}")

            logger.info(f"Finished TaskGroup for worker on {work_item.hash}")

            dolma_docs = []
            for task in dolma_tasks:
                try:
                    result = task.result()
                except Exception:
                    # some dolma doc creations may have failed
                    pass

                if result is not None:
                    dolma_docs.append(result)

            logger.info(f"Got {len(dolma_docs)} docs for {work_item.hash}")

            # === 优化后的输出逻辑，支持单文件和多文件/目录 ===
            pdf_paths = work_item.work_paths

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
            if args.only_markdown:
                suffix = f"_md_{timestamp}"
            else:
                suffix = f"_md+jsonl_{timestamp}"

            if len(pdf_paths) == 1:
                # 单文件：直接写到 PDF 同目录
                pdf_path = pdf_paths[0]
                pdf_dir = os.path.dirname(pdf_path)
                for doc in dolma_docs:
                    source_file = doc["metadata"]["Source-File"]
                    natural_text = doc["text"]
                    pdf_basename = os.path.splitext(os.path.basename(source_file))[0]
                    # 直接写到 pdf_dir
                    target_dir = pdf_dir
                    os.makedirs(target_dir, exist_ok=True)
                    # 写 md
                    if args.markdown_jsonl or args.only_markdown:
                        md_filename = f"{pdf_basename}.md"
                        markdown_path = os.path.join(target_dir, md_filename)
                        print(f"DEBUG: Writing md to {markdown_path}")
                        with open(markdown_path, "w") as md_f:
                            md_f.write(natural_text)
                        markdown_paths[source_file] = markdown_path  # 记录 Markdown 路径
                    # 写 jsonl（仅当不是 only_markdown 时）
                    if not args.only_markdown:
                        output_final_path = os.path.join(target_dir, f"output_{pdf_basename}.jsonl")
                        print(f"DEBUG: Writing jsonl to {output_final_path}")
                        with open(output_final_path, "w") as jf:
                            jf.write(json.dumps(doc, ensure_ascii=False) + "\n")
            else:
                from os.path import commonprefix, dirname, relpath, join, basename, splitext
                common_prefix = os.path.commonprefix(pdf_paths)
                if not os.path.isdir(common_prefix):
                    common_prefix = os.path.dirname(common_prefix)
                dir_name = os.path.basename(common_prefix.rstrip("/"))
                parent_dir = os.path.dirname(common_prefix.rstrip("/"))
                target_root_dir = os.path.join(parent_dir, f"{dir_name}{suffix}")
                os.makedirs(target_root_dir, exist_ok=True)

                for doc in dolma_docs:
                    source_file = doc["metadata"]["Source-File"]
                    natural_text = doc["text"]
                    pdf_basename = os.path.splitext(os.path.basename(source_file))[0]
                    # 计算相对路径
                    if len(pdf_paths) == 1:
                        rel_dir = ""
                    else:
                        rel_dir = os.path.dirname(os.path.relpath(source_file, common_prefix))
                    target_dir = os.path.join(target_root_dir, rel_dir)
                    os.makedirs(target_dir, exist_ok=True)
                    # 写 md
                    if args.markdown_jsonl or args.only_markdown:
                        md_filename = f"{pdf_basename}.md"
                        markdown_path = os.path.join(target_dir, md_filename)
                        print(f"DEBUG: Writing md to {markdown_path}")
                        with open(markdown_path, "w") as md_f:
                            md_f.write(natural_text)
                        markdown_paths[source_file] = markdown_path  # 记录 Markdown 路径
                    # 写 jsonl（仅当不是 only_markdown 时）
                    if not args.only_markdown:
                        output_final_path = os.path.join(target_dir, f"output_{pdf_basename}.jsonl")
                        with open(output_final_path, "w") as jf:
                            jf.write(json.dumps(doc, ensure_ascii=False) + "\n")

            if not args.only_markdown:
                metrics.add_metrics(
                    finished_input_tokens=sum(doc["metadata"]["total-input-tokens"] for doc in dolma_docs),
                    finished_output_tokens=sum(doc["metadata"]["total-output-tokens"] for doc in dolma_docs),
                )
                await work_queue.mark_done(work_item)
        except Exception as e:
            logger.exception(f"Exception occurred while processing work_hash {work_item.hash}: {e}")
        finally:
            semaphore.release()
    
    # 在所有工作完成后打印统计信息
    if all_pdf_paths:
        stats_data = summarize_directory_stats(list(all_pdf_paths), markdown_paths)
        print_stats_summary(stats_data)

async def sglang_server_task(model_name_or_path, args, semaphore):
    # Check GPU memory, lower mem devices need a bit less KV cache space because the VLM takes additional memory
    gpu_memory = torch.cuda.get_device_properties(0).total_memory / (1024**3)  # Convert to GB
    mem_fraction_arg = ["--mem-fraction-static", "0.80"] if gpu_memory < 60 else []

    cmd = [
        "python3",
        "-m",
        "sglang.launch_server",
        "--model-path",
        model_name_or_path,
        "--chat-template",
        args.model_chat_template,
        # "--context-length", str(args.model_max_context),  # Commented out due to crashes
        "--port",
        str(SGLANG_SERVER_PORT),
        "--log-level-http",
        "warning",
    ]
    cmd.extend(mem_fraction_arg)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Ensure the subprocess is terminated on exit
    def _kill_proc():
        proc.terminate()

    atexit.register(_kill_proc)

    # Shared variables between tasks
    last_running_req, last_queue_req = 0, 0
    server_printed_ready_message = False
    last_semaphore_release = time.time()

    async def process_line(line):
        nonlocal last_running_req, last_queue_req, last_semaphore_release, server_printed_ready_message
        sglang_logger.info(line)

        # if the server hasn't initialized yet, log all the lines to the main logger also, so that the user
        # can see any warnings/errors more easily
        if not server_printed_ready_message:
            logger.info(line)

        if "Detected errors during sampling" in line:
            logger.error("Cannot continue, sampling errors detected, model is probably corrupt")
            sys.exit(1)

        # TODO, need to trace down this issue in sglang itself, but it will otherwise cause the server to lock up
        if "IndexError: list index out of range" in line:
            logger.error("IndexError in model, restarting server")
            proc.terminate()

        if not server_printed_ready_message and "The server is fired up and ready to roll!" in line:
            server_printed_ready_message = True
            last_semaphore_release = time.time()

        match = re.search(r"#running-req: (\d+)", line)
        if match:
            last_running_req = int(match.group(1))

        match = re.search(r"#queue-req: (\d+)", line)
        if match:
            last_queue_req = int(match.group(1))
            logger.info(f"sglang running req: {last_running_req} queue req: {last_queue_req}")

    async def read_stream(stream):
        while True:
            line = await stream.readline()
            if not line:
                break
            try:
                line = line.decode("utf-8").rstrip()
                await process_line(line)
            except Exception as ex:
                logger.warning(f"Got {ex} when reading log line from inference server, skipping")

    async def timeout_task():
        nonlocal last_running_req, last_queue_req, last_semaphore_release
        try:
            while True:
                await asyncio.sleep(1)
                if server_printed_ready_message and last_queue_req == 0 and time.time() - last_semaphore_release > 30 and semaphore.locked():
                    semaphore.release()
                    last_semaphore_release = time.time()
                    logger.info("Semaphore released, allowing a worker to proceed.")
        except asyncio.CancelledError:
            pass  # Clean up if the task is cancelled

    # Start tasks to read stdout, stderr, and handle timeout logic
    stdout_task = asyncio.create_task(read_stream(proc.stdout))
    stderr_task = asyncio.create_task(read_stream(proc.stderr))
    timeout_task = asyncio.create_task(timeout_task())

    try:
        await proc.wait()
    except asyncio.CancelledError:
        logger.info("Got cancellation request for SGLang server")
        proc.terminate()
        raise

    timeout_task.cancel()
    await asyncio.gather(stdout_task, stderr_task, timeout_task, return_exceptions=True)


async def sglang_server_host(model_name_or_path, args, semaphore):
    MAX_RETRIES = 5
    retry = 0

    while retry < MAX_RETRIES:
        await sglang_server_task(model_name_or_path, args, semaphore)
        logger.warning("SGLang server task ended")
        retry += 1

    if retry >= MAX_RETRIES:
        logger.error(f"Ended up starting the sglang server more than {retry} times, cancelling pipeline")
        logger.error("")
        logger.error("Please make sure sglang is installed according to the latest instructions here: https://docs.sglang.ai/start/install.html")
        sys.exit(1)


async def sglang_server_ready():
    max_attempts = 300
    delay_sec = 1
    url = f"http://localhost:{SGLANG_SERVER_PORT}/v1/models"

    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient() as session:
                response = await session.get(url)

                if response.status_code == 200:
                    logger.info("sglang server is ready.")
                    return
                else:
                    logger.info(f"Attempt {attempt}: Unexpected status code {response.status_code}")
        except Exception:
            logger.warning(f"Attempt {attempt}: Please wait for sglang server to become ready...")

        await asyncio.sleep(delay_sec)

    raise Exception("sglang server did not become ready after waiting.")


async def download_model(model_name_or_path: str):
    if model_name_or_path.startswith("s3://") or model_name_or_path.startswith("gs://") or model_name_or_path.startswith("weka://"):
        logger.info(f"Downloading model directory from '{model_name_or_path}'")
        model_cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "olmocr", "model")
        download_directory([model_name_or_path], model_cache_dir)
        return model_cache_dir
    elif os.path.isabs(model_name_or_path) and os.path.isdir(model_name_or_path):
        logger.info(f"Using local model path at '{model_name_or_path}'")
        return model_name_or_path
    else:
        logger.info(f"Downloading model with hugging face '{model_name_or_path}'")
        snapshot_download(repo_id=model_name_or_path)
        return model_name_or_path


async def metrics_reporter(work_queue):
    while True:
        # Leading newlines preserve table formatting in logs
        logger.info(f"Queue remaining: {work_queue.size}")
        logger.info("\n" + str(metrics))
        logger.info("\n" + str(await tracker.get_status_table()))
        await asyncio.sleep(10)


def submit_beaker_job(args):
    from beaker import (  # type: ignore
        Beaker,
        Constraints,
        EnvVar,
        ExperimentSpec,
        ImageSource,
        Priority,
        ResultSpec,
        SecretNotFound,
        TaskContext,
        TaskResources,
        TaskSpec,
    )

    b = Beaker.from_env(default_workspace=args.beaker_workspace)
    account = b.account.whoami()
    owner = account.name
    beaker_image = f"jakep/olmocr-inference-{VERSION}"

    task_name = f"olmocr-{os.path.basename(args.workspace.rstrip('/'))}"

    # Take out --beaker flag so the workers will just run things
    args_list = [arg for arg in sys.argv[1:] if arg != "--beaker"]

    # Take out the --pdfs [arg] or --pdfs=[arg], since the queue is populated locally
    args_list = [arg for i, arg in enumerate(args_list) if not (arg.startswith("--pdfs") or (i > 0 and args_list[i - 1] == "--pdfs"))]

    try:
        b.secret.get(f"{owner}-WEKA_ACCESS_KEY_ID", args.beaker_workspace)
        b.secret.get(f"{owner}-WEKA_SECRET_ACCESS_KEY", args.beaker_workspace)
        b.secret.get(f"{owner}-AWS_CREDENTIALS_FILE", args.beaker_workspace)
    except SecretNotFound:
        print(
            f"Expected beaker secrets for accessing Weka and S3 are not found. Are you okay to write those to your beaker workspace {args.beaker_workspace}? [y/n]"
        )

        if input().strip().lower() != "y":
            print("Exiting...")
            sys.exit(1)

        b.secret.write(f"{owner}-WEKA_ACCESS_KEY_ID", os.environ.get("WEKA_ACCESS_KEY_ID", ""), args.beaker_workspace)
        b.secret.write(f"{owner}-WEKA_SECRET_ACCESS_KEY", os.environ.get("WEKA_SECRET_ACCESS_KEY", ""), args.beaker_workspace)
        b.secret.write(
            f"{owner}-AWS_CREDENTIALS_FILE",
            open(os.path.join(os.path.expanduser("~"), ".aws", "credentials")).read(),
            args.beaker_workspace,
        )

    env_var_secrets = [
        EnvVar(name="WEKA_ACCESS_KEY_ID", secret=f"{owner}-WEKA_ACCESS_KEY_ID"),
        EnvVar(name="WEKA_SECRET_ACCESS_KEY", secret=f"{owner}-WEKA_SECRET_ACCESS_KEY"),
        EnvVar(name="AWS_CREDENTIALS_FILE", secret=f"{owner}-AWS_CREDENTIALS_FILE"),
    ]

    try:
        b.secret.get("OLMOCR_PREVIEW_HF_TOKEN", args.beaker_workspace)
        env_var_secrets.append(EnvVar(name="HF_TOKEN", secret="OLMOCR_PREVIEW_HF_TOKEN"))
    except SecretNotFound:
        pass

    try:
        b.secret.get("OE_DATA_GCS_SA_KEY", args.beaker_workspace)
        env_var_secrets.append(EnvVar(name="GOOGLE_APPLICATION_CREDENTIALS_FILE", secret="OE_DATA_GCS_SA_KEY"))
    except SecretNotFound:
        print("Input the olmo-gcs SA key if you would like to load weights from gcs (end with a double newline):")
        lines = []
        prev_empty = False
        for line in iter(input, None):
            if not line and prev_empty:
                break
            prev_empty = not line
            lines.append(line)
        gcs_sa_key = "\n".join(lines[:-1]).strip()  # Remove the last empty line
        if gcs_sa_key:
            b.secret.write("OE_DATA_GCS_SA_KEY", gcs_sa_key, args.beaker_workspace)
            env_var_secrets.append(EnvVar(name="GOOGLE_APPLICATION_CREDENTIALS_FILE", secret="OE_DATA_GCS_SA_KEY"))

    # Create the experiment spec
    experiment_spec = ExperimentSpec(
        budget="ai2/oe-data",
        description=task_name,
        tasks=[
            TaskSpec(
                name=task_name,
                propagate_failure=False,
                propagate_preemption=False,
                replicas=args.beaker_gpus,
                context=TaskContext(
                    priority=Priority(args.beaker_priority),
                    preemptible=True,
                ),
                image=ImageSource(beaker=beaker_image),
                command=["python", "-m", "olmocr.pipeline"] + args_list,
                env_vars=[EnvVar(name="BEAKER_JOB_NAME", value=task_name), EnvVar(name="OWNER", value=owner)] + env_var_secrets,
                resources=TaskResources(gpu_count=1),
                constraints=Constraints(cluster=args.beaker_cluster if isinstance(args.beaker_cluster, list) else [args.beaker_cluster]),
                result=ResultSpec(path="/noop-results"),
            )
        ],
    )

    experiment_data = b.experiment.create(spec=experiment_spec, workspace=args.beaker_workspace)

    print(f"Experiment URL: https://beaker.org/ex/{experiment_data.id}")


def print_stats(args, root_work_queue):
    LONG_CONTEXT_THRESHOLD = 32768

    assert args.workspace.startswith("s3://"), "Printing stats functionality only works with s3 workspaces for now."

    # Get total work items and completed items
    index_file_s3_path = os.path.join(args.workspace, "work_index_list.csv.zstd")
    output_glob = os.path.join(args.workspace, "results", "*.jsonl")

    done_work_items = expand_s3_glob(workspace_s3, output_glob)
    work_queue_lines = download_zstd_csv(workspace_s3, index_file_s3_path)

    work_queue = {}
    for line in work_queue_lines:
        if line.strip():
            parts = root_work_queue._decode_csv_row(line.strip())
            if parts:  # Ensure we have at least one part
                work_queue[parts[0]] = parts[1:]

    total_items = len(work_queue)
    completed_items = len(done_work_items)

    def process_output_file(s3_path):
        try:
            data = get_s3_bytes(workspace_s3, s3_path)
            doc_count = 0
            total_input_tokens = 0
            total_output_tokens = 0
            total_pages = 0
            total_fallback_pages = 0
            processed_paths = set()

            # Counters for long context docs within a single file
            long_context_docs = 0
            long_context_tokens = 0

            for line in data.decode("utf-8").splitlines():
                if line.strip():
                    doc = json.loads(line)
                    doc_count += 1
                    doc_input_tokens = doc["metadata"].get("total-input-tokens", 0)
                    doc_output_tokens = doc["metadata"].get("total-output-tokens", 0)
                    doc_pages = doc["metadata"].get("pdf-total-pages", 0)
                    doc_fallback_pages = doc["metadata"].get("total-fallback-pages", 0)

                    total_input_tokens += doc_input_tokens
                    total_output_tokens += doc_output_tokens
                    total_pages += doc_pages
                    total_fallback_pages += doc_fallback_pages
                    processed_paths.add(doc["metadata"]["Source-File"])

                    # Check if this doc exceeds the long context threshold
                    if doc_output_tokens > LONG_CONTEXT_THRESHOLD:
                        long_context_docs += 1
                        long_context_tokens += doc_output_tokens

            return (
                doc_count,
                total_input_tokens,
                total_output_tokens,
                total_pages,
                total_fallback_pages,
                processed_paths,
                long_context_docs,
                long_context_tokens,
            )
        except Exception as e:
            logger.warning(f"Error processing {s3_path}: {e}")
            return 0, 0, 0, 0, 0, set(), 0, 0

    print("\nProcessing output files...")
    docs_total = 0
    input_tokens_total = 0
    output_tokens_total = 0
    pages_total = 0
    fallback_pages_total = 0
    all_processed_paths = set()
    original_paths = set()

    # Counters for long context documents across all files
    long_context_docs_count = 0
    long_context_tokens_total = 0

    # First collect all original PDF paths
    for done_work_item in done_work_items:
        if match := re.search(r"output_(\w+).jsonl", done_work_item):
            done_work_hash = match.group(1)
            if done_work_hash in work_queue:
                original_paths.update(work_queue[done_work_hash])

    with ThreadPoolExecutor() as executor:
        futures = {executor.submit(process_output_file, item): item for item in done_work_items}

        for future in tqdm(as_completed(futures), total=len(futures)):
            (doc_count, input_tokens, output_tokens, pages, fallback_pages, processed_paths, long_context_docs, long_context_tokens) = future.result()
            docs_total += doc_count
            input_tokens_total += input_tokens
            output_tokens_total += output_tokens
            pages_total += pages
            fallback_pages_total += fallback_pages
            all_processed_paths.update(processed_paths)
            long_context_docs_count += long_context_docs
            long_context_tokens_total += long_context_tokens

    skipped_paths = original_paths - all_processed_paths

    print("\nWork Items Status:")
    print(f"Total work items: {total_items:,}")
    print(f"Completed items: {completed_items:,}")
    print(f"Remaining items: {total_items - completed_items:,}")

    print("\nResults:")
    print(f"Total documents processed: {docs_total:,}")
    print(f"Total documents skipped: {len(skipped_paths):,}")
    print(f"Total pages on fallback: {fallback_pages_total:,}")
    print(f"Total pages processed: {pages_total:,}")

    print(f"\nTotal output tokens: {output_tokens_total:,}")
    print(f"Projected output tokens: {round((output_tokens_total/max(1, completed_items))*total_items):,}")

    print(f"\nAverage pages per doc: {pages_total/max(1,docs_total):,.1f}")
    print(f"Average output tokens per doc: {output_tokens_total/max(1,docs_total):,.1f}")
    print(f"Average output tokens per page: {output_tokens_total/max(1,pages_total):,.1f}")

    # Print long context documents stats
    print(f"\nLong Context Documents (>{LONG_CONTEXT_THRESHOLD} tokens): {long_context_docs_count:,}")
    print(f"Total tokens in long context documents: {long_context_tokens_total:,}")

#  新增工具函数   递归收集 PDF 文件
def collect_pdf_files(paths):
    import glob
    pdf_files = set()
    for path in paths:
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                for file in files:
                    if file.lower().endswith(".pdf"):
                        pdf_files.add(os.path.join(root, file))
        elif "*" in path or "?" in path or "[" in path:
            pdf_files |= set(glob.glob(path, recursive=True))
        elif os.path.isfile(path) and path.lower().endswith(".pdf"):
            pdf_files.add(path)
    return pdf_files

def clean_workspace_queue_files(workspace_path):
    if not workspace_path.startswith("s3://"):
        queue_patterns = [
            "work_index_list.csv.zstd",
            "local_queue.json",
            "work_index_list.csv",
            "work_index_list.csv.zst",
            "work_index_list.csv.gz",
        ]
        for pattern in queue_patterns:
            for file in glob.glob(os.path.join(workspace_path, pattern)):
                try:
                    os.remove(file)
                    print(f"DEBUG: Removed old queue file: {file}")
                except Exception as e:
                    print(f"DEBUG: Failed to remove {file}: {e}")
                    
def clean_all_workspace_files(workspace_path):
    import shutil
    # 清理队列文件
    clean_workspace_queue_files(workspace_path)
    # 清理 results 目录下所有文件
    results_dir = os.path.join(workspace_path, "results")
    if os.path.exists(results_dir):
        shutil.rmtree(results_dir)
    # 清理 worker_locks 目录
    locks_dir = os.path.join(workspace_path, "worker_locks")
    if os.path.exists(locks_dir):
        shutil.rmtree(locks_dir)
    # 清理 workspace 下所有状态文件
    for file in os.listdir(workspace_path):
        file_path = os.path.join(workspace_path, file)
        if os.path.isfile(file_path) and (
            file_path.endswith(".jsonl")
            or file_path.endswith(".md")
            or file_path.endswith(".csv")
            or file_path.endswith(".zstd")
            or file_path.endswith(".json")
            or file_path.endswith(".gz")
            or file_path.endswith(".zst")
            or file_path.endswith(".done")
            or file_path.endswith(".index")
            or file_path.endswith(".cache")
            or file_path.endswith(".lock")
            or file_path.endswith(".tmp")
        ):
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"Failed to remove {file_path}: {e}")            
             
import os

def has_old_queue_files(workspace_path):
    queue_patterns = [
        "work_index_list.csv.zstd",
        "local_queue.json",
        "work_index_list.csv",
        "work_index_list.csv.zst",
        "work_index_list.csv.gz",
    ]
    for pattern in queue_patterns:
        for file in glob.glob(os.path.join(workspace_path, pattern)):
            if os.path.exists(file):
                return True
    return False                    

async def main():
    # 加载本地环境变量，确保配置（如 API 密钥）正确加载
    load_dotenv(
        dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), "local.env"),
        override=True  # 强制覆盖已有环境变量
    )
    # 调试输出：打印远程 API 配置信息
    print("DEBUG: REMOTE_API_BASE =", os.getenv("OPENAI_API_BASE"))
    print("DEBUG: REMOTE_API_PATH =", os.getenv("OPENAI_API_PATH"))
    print("DEBUG: REMOTE_API_KEY =", os.getenv("OPENAI_API_KEY"))

    # 初始化命令行参数解析器
    parser = argparse.ArgumentParser(description="Manager for running millions of PDFs through a batch inference pipeline")
    # 添加远程 API 模式开关
    parser.add_argument("--use_remote_api", action="store_true", help="使用远程 API 而非本地 LLM")
    # 工作空间路径，可选，默认为当前目录
    parser.add_argument(
        "workspace",
        nargs="?",
        default=".",
        help="工作存储路径，可为本地文件夹或 S3 路径（如 s3://bucket/prefix/）。默认：当前目录",
    )
    # PDF 文件路径，支持通配符或文件列表
    parser.add_argument(
        "--pdfs",
        nargs="*",
        help="添加存储在 S3 的 PDF 路径，支持通配符（如 s3://bucket/prefix/*.pdf）或包含 PDF 路径的列表文件",
        default=None,
    )
    parser.add_argument("--workspace_profile", help="访问工作空间的 S3 配置 profile", default=None)
    parser.add_argument("--pdf_profile", help="访问原始 PDF 文档的 S3 配置 profile", default=None)
    parser.add_argument("--pages_per_group", type=int, default=500, help="每个工作项组的目标 PDF 页面数")
    parser.add_argument("--max_page_retries", type=int, default=8, help="页面渲染的最大重试次数")
    parser.add_argument("--max_page_error_rate", type=float, default=0.004, help="文档中允许的页面失败率，默认 1/250")
    parser.add_argument("--workers", type=int, default=8, help="同时运行的 worker 数量")
    parser.add_argument("--apply_filter", action="store_true", help="应用过滤器，仅保留英文 PDF，非表单且非 SEO 垃圾内容")
    parser.add_argument("--stats", action="store_true", help="不运行任务，仅报告当前工作空间的统计信息")
    parser.add_argument("--markdown_jsonl", action="store_true", help="同时写入 Markdown 和 JSONL 文件（保留文件夹结构）")
    # 添加仅输出 Markdown 文件的选项
    parser.add_argument("--only_markdown", action="store_true", help="仅写入 Markdown 文件，不写入 JSONL 文件")

    # 模型相关参数
    parser.add_argument(
        "--model",
        help="模型路径，支持多个路径，脚本会选择最快访问的路径",
        default="allenai/olmOCR-7B-0225-preview",
    )
    parser.add_argument("--model_max_context", type=int, default="8192", help="模型微调的最大上下文长度")
    parser.add_argument("--model_chat_template", type=str, default="qwen2-vl", help="传递给 SGLang 服务器的聊天模板")
    parser.add_argument("--target_longest_image_dim", type=int, help="PDF 页面渲染的最长边尺寸", default=1024)
    parser.add_argument("--target_anchor_text_len", type=int, help="锚文本的最大字符数", default=6000)

    # Beaker 任务参数
    parser.add_argument("--beaker", action="store_true", help="将任务提交到 Beaker 而非本地运行")
    parser.add_argument("--beaker_workspace", help="提交任务的 Beaker 工作空间", default="ai2/olmocr")
    parser.add_argument(
        "--beaker_cluster",
        help="运行任务的 Beaker 集群",
        default=["ai2/jupiter-cirrascale-2", "ai2/ceres-cirrascale", "ai2/neptune-cirrascale", "ai2/saturn-cirrascale", "ai2/augusta-google-1"],
    )
    parser.add_argument("--beaker_gpus", type=int, default=1, help="运行的 GPU 副本数量")
    parser.add_argument("--beaker_priority", type=str, default="normal", help="Beaker 任务优先级")
    parser.add_argument("--port", type=int, default=30024, help="SGLang 服务器使用的端口")

    # 解析命令行参数
    args = parser.parse_args()

    # 自动化清理：如果指定了 PDF 文件路径，清理旧的工作空间队列文件
    if args.pdfs:
        clean_all_workspace_files(args.workspace)

    # 检查参数冲突：--markdown_jsonl 和 --only_markdown 不能同时使用
    if args.markdown_jsonl and args.only_markdown:
        parser.error("不能同时使用 --markdown_jsonl 和 --only_markdown。")

    # 设置全局 S3 客户端和 SGLang 服务器端口
    global workspace_s3, pdf_s3, SGLANG_SERVER_PORT
    SGLANG_SERVER_PORT = args.port

    # 配置 Beaker 环境（如果在 Beaker 中运行）
    if "BEAKER_JOB_NAME" in os.environ:
        sglang_logger.addHandler(console_handler)
        cred_path = os.path.join(os.path.expanduser("~"), ".aws", "credentials")
        os.makedirs(os.path.dirname(cred_path), exist_ok=True)
        with open(cred_path, "w") as f:
            f.write(os.environ.get("AWS_CREDENTIALS_FILE"))
        cred_path = os.path.join(os.path.expanduser("~"), ".gcs", "credentials")
        os.makedirs(os.path.dirname(cred_path), exist_ok=True)
        with open(cred_path, "w") as f:
            f.write(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_FILE"))
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = cred_path
        workspace_s3 = boto3.client("s3")
        pdf_s3 = boto3.client("s3")

        # 错开模型下载时间，避免所有 Beaker 任务同时下载
        replica_count = int(os.environ.get("BEAKER_REPLICA_COUNT", "1"))
        interval = 10 if (replica_count - 1) * 10 <= 240 else 240 / max(1, replica_count - 1)
        sleep_time = int(int(os.environ.get("BEAKER_REPLICA_RANK", "0")) * interval)
        logger.info(f"Beaker 任务休眠 {sleep_time} 秒以错开模型下载")
        await asyncio.sleep(sleep_time)

    # 根据配置文件设置 S3 客户端
    if args.workspace_profile:
        workspace_session = boto3.Session(profile_name=args.workspace_profile)
        workspace_s3 = workspace_session.client("s3")
    if args.pdf_profile:
        pdf_session = boto3.Session(profile_name=args.pdf_profile)
        pdf_s3 = pdf_session.client("s3")

    # 检查 Poppler 版本（用于加载 PDF）
    check_poppler_version()

    # 创建工作队列，根据工作空间类型选择 S3 或本地队列
    if args.workspace.startswith("s3://"):
        work_queue = S3WorkQueue(workspace_s3, args.workspace)
    else:
        work_queue = LocalWorkQueue(args.workspace)

    # 处理 PDF 文件路径，填充工作队列
    if args.pdfs:
        logger.info("收到 --pdfs 参数，将添加到工作队列")
        pdf_work_paths = collect_pdf_files(args.pdfs)
        logger.info(f"找到 {len(pdf_work_paths):,} 个 PDF 路径待添加")

        # 估算平均每 PDF 的页面数
        sample_size = min(100, len(pdf_work_paths))
        sampled_pdfs = random.sample(list(pdf_work_paths), sample_size)
        page_counts = []
        for pdf in tqdm(sampled_pdfs, desc="采样 PDF 以计算平均页面数"):
            try:
                with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp_file:
                    tmp_file.write(get_s3_bytes(pdf_s3, pdf))
                    tmp_file.flush()
                    if is_png(tmp_file.name) or is_jpeg(tmp_file.name):
                        page_counts.append(1)
                    else:
                        reader = PdfReader(tmp_file.name)
                        page_counts.append(len(reader.pages))
            except Exception as e:
                logger.warning(f"无法读取 {pdf}：{e}")
        if page_counts:
            avg_pages_per_pdf = sum(page_counts) / len(page_counts)
        else:
            logger.warning("无法读取任何 PDF 以估算平均页面数。")
            avg_pages_per_pdf = 10  # 默认 10 页
        items_per_group = max(1, int(args.pages_per_group / avg_pages_per_pdf))
        logger.info(f"根据平均页面数 {avg_pages_per_pdf:.2f} 计算 items_per_group：{items_per_group}")

        # 填充工作队列
        await work_queue.populate_queue(pdf_work_paths, items_per_group)
        qsize = await work_queue.initialize_queue()
        logger.info(f"DEBUG：初始化工作队列大小：{qsize}")

    # 如果指定了 --stats，仅打印统计信息并退出
    if args.stats:
        print_stats(args, work_queue)
        return

    # 如果指定了 --beaker，提交 Beaker 任务并退出
    if args.beaker:
        submit_beaker_job(args)
        return

    # 如果未使用远程 API，检查本地 LLM 依赖
    if not args.use_remote_api:
        check_sglang_version()
        check_torch_gpu_available()

    # 记录管道启动
    logger.info(f"启动管道，进程 ID：{os.getpid()}")

    # 根据模式初始化模型和信号量
    if not args.use_remote_api:
        # 本地 LLM 模式：下载模型并启动 SGLang 服务器
        model_name_or_path = await download_model(args.model)
        semaphore = asyncio.Semaphore(1)
        sglang_server = asyncio.create_task(sglang_server_host(model_name_or_path, args, semaphore))
        await sglang_server_ready()
    else:
        # 远程 API 模式：无需模型或服务器
        model_name_or_path = None
        semaphore = asyncio.Semaphore(1)
        sglang_server = None

    # 记录文档解析和 Markdown 输出开始时间
    start_time = time.time()
    logger.info("开始文档解析和 Markdown 输出")

    # 启动主处理流程
    metrics_task = asyncio.create_task(metrics_reporter(work_queue))
    worker_tasks = []
    for i in range(args.workers):
        task = asyncio.create_task(worker(args, work_queue, semaphore, worker_id=i))
        worker_tasks.append(task)

    # 等待所有 worker 任务完成
    await asyncio.gather(*worker_tasks)

    # 记录结束时间并输出总耗时
    end_time = time.time()
    total_time = end_time - start_time
    logger.info(f"文档解析到 Markdown 输出的总处理时间：{total_time:.2f} 秒")

    # 清理资源
    process_pool.shutdown(wait=False)
    if sglang_server is not None:
        sglang_server.cancel()
    metrics_task.cancel()
    logger.info("工作完成")
  


if __name__ == "__main__":
    asyncio.run(main())