import json

with open("/Users/wingzheng/Desktop/github/ParseDoc/olmocr/tests/test_request_json/b64img.txt") as f:
    b64img = f.read().strip()

data = {
    "model": "gpt-4.1",
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "请描述这张图片"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64img}"}}
            ]
        }
    ],
    "max_tokens": 3000,
    "temperature": 0.0
}

with open("request.json", "w") as f:
    json.dump(data, f, ensure_ascii=False)