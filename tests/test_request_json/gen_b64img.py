from olmocr.data.renderpdf import render_pdf_to_base64png

# 路径和页码按需修改
pdf_path = "/Users/wingzheng/Desktop/github/ParseDoc/olmocr/tests/gnarly_pdfs/horribleocr.pdf"
page_num = 1
target_dim = 1024

b64img = render_pdf_to_base64png(pdf_path, page_num, target_longest_image_dim=target_dim)
print(b64img)