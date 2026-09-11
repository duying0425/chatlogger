"""飞书云文档快照缓存。

同步消息时识别其中分享的飞书云文档链接（docx / wiki），
通过 docx Blocks API 拉取文档内容并转换为 Markdown，
快照保存到 cache/{chat}/assets/docs/ 供离线阅读。
"""
import json
import re
import time
from config import Config

# 飞书云文档链接：https://xxx.feishu.cn/docx/<token> 或 /wiki/<token>（旧版 /docs/<token>）
# token 为字母数字组合；URL 可能带 ?from=... 等查询参数
DOC_URL_RE = re.compile(
    r"https?://[A-Za-z0-9.-]*feishu\.cn/(docx|wiki|docs)/([A-Za-z0-9]+)", re.IGNORECASE)

# 支持 URL 链接形式的云文档类型（doc 为旧版文档，API 与 docx 不同，仅记录链接）
URL_KIND_DOCX = "docx"
URL_KIND_WIKI = "wiki"

# 代码块语言枚举（飞书 docx code block style.language）
CODE_LANG_MAP = {
    1: "", 2: "abap", 3: "ada", 4: "apache", 5: "apex", 6: "asm", 7: "bash",
    8: "csharp", 9: "cpp", 10: "c", 11: "cobol", 12: "css", 13: "coffeescript",
    14: "d", 15: "dart", 16: "delphi", 17: "django", 18: "dockerfile",
    19: "erlang", 20: "fortran", 22: "go", 23: "groovy", 24: "html",
    26: "http", 27: "haskell", 28: "json", 29: "java", 30: "javascript",
    31: "julia", 32: "kotlin", 33: "latex", 34: "lisp", 36: "lua", 37: "matlab",
    38: "makefile", 39: "markdown", 40: "nginx", 41: "objectivec", 43: "php",
    44: "perl", 46: "powershell", 48: "protobuf", 49: "python", 50: "r",
    52: "ruby", 53: "rust", 56: "sql", 57: "scala", 59: "shell", 60: "swift",
    61: "thrift", 62: "typescript", 63: "vbscript", 64: "vb", 65: "xml",
    66: "yaml", 67: "cmake", 68: "diff",
}

# block_type 枚举 → 名称（用于渲染与不支持块提示）
BLOCK_TYPE_NAME = {
    1: "page", 2: "text", 3: "heading1", 4: "heading2", 5: "heading3",
    6: "heading4", 7: "heading5", 8: "heading6", 9: "heading7",
    10: "heading8", 11: "heading9", 12: "bullet", 13: "ordered", 14: "code",
    15: "quote", 16: "equation", 17: "todo", 18: "bitable", 19: "callout",
    20: "chat_card", 21: "diagram", 22: "divider", 23: "file", 24: "grid",
    25: "grid_column", 26: "iframe", 27: "image", 28: "isv", 29: "mindnote",
    30: "sheet", 31: "table", 32: "table_cell", 33: "view",
    34: "quote_container", 35: "task", 36: "okr", 37: "okr_objective",
    38: "okr_key_result", 40: "add_ons", 41: "jira_issue",
}


def extract_doc_links(msg):
    """从消息中提取飞书云文档链接。

    返回 [{"kind": "docx"|"wiki"|"doc", "token": "..."}]，按出现顺序去重。
    直接在消息原始 content JSON 字符串上做正则，text / post 的 <a> 链接都能覆盖。
    """
    body = msg.get("body", {}) if isinstance(msg, dict) else {}
    content = body.get("content", "") if isinstance(body, dict) else ""
    if not content:
        return []
    # content 是 JSON 字符串；直接全文正则，无需区分消息类型
    # （URL 中的 / 会被 JSON 转义为 \/，先还原）
    text = content.replace("\\/", "/")
    seen = set()
    links = []
    for m in DOC_URL_RE.finditer(text):
        kind_raw, token = m.group(1).lower(), m.group(2)
        if not token or token in seen:
            continue
        seen.add(token)
        kind = URL_KIND_WIKI if kind_raw == "wiki" else (
            "doc" if kind_raw == "docs" else URL_KIND_DOCX)
        links.append({"kind": kind, "token": token})
    return links


# ===== Block → Markdown =====

def _inline_elements_to_md(text_obj):
    """把 Text Block 的 elements 转为行内 Markdown。"""
    if not isinstance(text_obj, dict):
        return ""
    parts = []
    for elem in text_obj.get("elements", []):
        if not isinstance(elem, dict):
            continue
        run = elem.get("text_run")
        if isinstance(run, dict):
            content = run.get("content", "")
            if not content:
                continue
            style = run.get("text_element_style") or {}
            # 链接样式：[文本](url)
            link = style.get("link") or {}
            url = link.get("url", "") if isinstance(link, dict) else ""
            # 行内样式
            if style.get("inline_code"):
                content = f"`{content}`"
            else:
                if style.get("bold"):
                    content = f"**{content}**"
                if style.get("italic"):
                    content = f"*{content}*"
                if style.get("strikethrough"):
                    content = f"~~{content}~~"
                if style.get("underline"):
                    content = f"<u>{content}</u>"
            if url:
                parts.append(f"[{content}]({url})")
            else:
                parts.append(content)
        elif isinstance(elem.get("mention_user"), dict):
            parts.append("@成员")
        elif isinstance(elem.get("mention_doc"), dict):
            md = elem["mention_doc"]
            url = md.get("url", "")
            parts.append(f"[📄 文档]({url})" if url else "[📄 文档]")
        elif elem.get("reminder") is not None:
            parts.append("[提醒]")
        elif elem.get("file") is not None:
            parts.append("[📎 附件]")
    return "".join(parts)


def _escape_table_cell(text):
    """表格单元格内的文本：管道符转义、换行转 <br>。"""
    return text.replace("|", "\\|").replace("\n", "<br>")


class DocxConverter:
    """把 docx Block 列表转换为 Markdown。

    image_saver: callable(image_token) -> 相对路径 或 None（下载失败/未开启）
    """

    def __init__(self, blocks, image_saver=None, max_blocks=None):
        self.blocks_by_id = {b.get("block_id"): b for b in blocks if isinstance(b, dict)}
        self.image_saver = image_saver or (lambda token: None)
        self.max_blocks = max_blocks or getattr(Config, "MAX_DOC_BLOCKS", 2000)
        self.rendered_count = 0
        self.truncated = False

    def convert(self):
        """返回 Markdown 字符串。"""
        # 根节点是 page block（block_type=1，无 parent_id）
        root = None
        for b in self.blocks_by_id.values():
            if b.get("block_type") == 1 and not b.get("parent_id"):
                root = b
                break
        if root is None:
            # 兜底：取第一个 block 的 children 根
            first = next(iter(self.blocks_by_id.values()), None)
            root = first
        if root is None:
            return ""
        lines = self._render_children(root, indent=0, ordered_counters=[])
        md = "\n\n".join(l for l in lines if l is not None and l != "")
        if self.truncated:
            md += f"\n\n> ⚠️ 文档过长（超过 {self.max_blocks} 个内容块），已截断"
        return md

    # -- 遍历 --

    def _render_children(self, block, indent, ordered_counters):
        """渲染 block 的子块列表。ordered_counters: 每层有序列表计数（None 表示非有序）。"""
        lines = []
        children = block.get("children") or []
        n_children = len(children)
        for i, child_id in enumerate(children):
            if self.rendered_count >= self.max_blocks:
                self.truncated = True
                break
            child = self.blocks_by_id.get(child_id)
            if child is None:
                continue
            # 有序列表编号：同层连续 ordered 递增，否则重置为 1
            counters = list(ordered_counters)
            while len(counters) <= indent:
                counters.append(None)
            prev = self.blocks_by_id.get(children[i - 1]) if i > 0 else None
            prev_is_ordered = bool(prev) and prev.get("block_type") == 13
            if child.get("block_type") == 13:
                if not prev_is_ordered:
                    counters[indent] = 0
                counters[indent] = (counters[indent] or 0) + 1
            else:
                counters[indent] = None
            line = self._render_block(child, indent, counters)
            # 列表/待办块可能嵌套子列表：子块缩进一层渲染
            if child.get("children") and child.get("block_type") in (12, 13, 17):
                nested = self._render_children(child, indent + 1, counters)
                if nested:
                    line = (line or f"{pad}-") + "\n" + "\n".join(nested)
            if line:  # None/"" 跳过（空块）
                lines.append(line)
        return lines

    def _render_block(self, block, indent, counters):
        self.rendered_count += 1
        btype = block.get("block_type")
        pad = "  " * indent

        # 文本类块（text/heading/bullet/ordered/quote/todo/code/equation）
        if btype in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 17):
            key = BLOCK_TYPE_NAME.get(btype, "text")
            text_obj = block.get(key) or {}
            inline = _inline_elements_to_md(text_obj).strip()

            if btype == 2:  # text
                return inline or None
            if 3 <= btype <= 11:  # heading1-9
                level = btype - 2
                return f"{'#' * level} {inline}" if inline else None
            if btype == 12:  # bullet
                return f"{pad}- {inline}" if inline else f"{pad}-"
            if btype == 13:  # ordered
                num = counters[indent] if indent < len(counters) and counters[indent] else 1
                return f"{pad}{num}. {inline}" if inline else f"{pad}{num}."
            if btype == 14:  # code
                style = text_obj.get("style") or {}
                lang = CODE_LANG_MAP.get(style.get("language"), "")
                code = "".join(
                    (r.get("text_run") or {}).get("content", "")
                    for r in text_obj.get("elements", [])
                    if isinstance(r, dict))
                return f"```{lang}\n{code.rstrip()}\n```"
            if btype == 15:  # quote
                return f"> {inline}" if inline else None
            if btype == 17:  # todo
                done = (text_obj.get("style") or {}).get("done", False)
                mark = "x" if done else " "
                return f"{pad}- [{mark}] {inline}" if inline else f"{pad}- [{mark}]"
            return inline or None

        if btype == 16:  # equation（key 不在文本类元组里，单独兜底）
            text_obj = block.get("equation") or {}
            inline = _inline_elements_to_md(text_obj).strip()
            return f"$$ {inline} $$" if inline else None

        if btype == 22:  # divider
            return "---"

        if btype == 27:  # image
            img = block.get("image") or {}
            token = img.get("token", "")
            rel = self.image_saver(token) if token else None
            if rel:
                return f"![图片]({rel})"
            return "[图片]"

        if btype == 31:  # table
            return self._render_table(block)

        if btype == 26:  # iframe
            url = (block.get("iframe") or {}).get("url", "")
            return f"[🔗 嵌入链接]({url})" if url else "[嵌入内容]"

        if btype == 23:  # file
            return "[📎 文件]"

        if btype in (19, 34):  # callout / quote_container：子块按引用渲染
            inner = self._render_children(block, indent, counters)
            if not inner:
                return None
            quoted = "\n".join("> " + l for l in inner)
            return f"{pad}{quoted}" if pad else quoted

        if btype in (24, 25, 33):  # grid / grid_column / view：透传子块
            inner = self._render_children(block, indent, counters)
            return "\n\n".join(inner) if inner else None

        if btype == 18:
            return "[📊 多维表格]"
        if btype == 29:
            return "[🧠 思维笔记]"
        if btype == 30:
            return "[📊 电子表格]"
        if btype == 21:
            return "[🎨 流程图]"
        if btype in (20, 28, 35, 36, 37, 38, 40, 41):
            return f"[{BLOCK_TYPE_NAME.get(btype, '未知')} 内容块]"

        # 未识别的块：若带文本数据则尽量渲染，否则给占位提示
        for key in ("text", "heading1", "heading2", "heading3"):
            if isinstance(block.get(key), dict):
                return _inline_elements_to_md(block[key]).strip() or None
        return f"[不支持的内容块: {BLOCK_TYPE_NAME.get(btype, btype)}]"

    def _render_table(self, block):
        """表格 → GFM 表格。cells 为行优先的 table_cell block id 列表。"""
        table = block.get("table") or {}
        prop = table.get("property") or {}
        row_size = int(prop.get("row_size") or 0)
        col_size = int(prop.get("column_size") or 0)
        # cells 可能在 table.cells 或 table.property.cells（不同版本结构不同）
        cell_ids = table.get("cells") or prop.get("cells") or []
        if not cell_ids:
            cell_ids = block.get("children") or []
        if not row_size or not col_size or len(cell_ids) < row_size * col_size:
            return "[表格]"

        rows = []
        for r in range(row_size):
            row_cells = []
            for c in range(col_size):
                cid = cell_ids[r * col_size + c] if r * col_size + c < len(cell_ids) else None
                cell_block = self.blocks_by_id.get(cid) if cid else None
                row_cells.append(self._cell_text(cell_block))
            rows.append(row_cells)

        # GFM 表格：首行为表头
        header = rows[0]
        body = rows[1:] if len(rows) > 1 else [[""] * col_size]
        lines = [
            "| " + " | ".join(_escape_table_cell(x) for x in header) + " |",
            "| " + " | ".join(["---"] * col_size) + " |",
        ]
        for row in body:
            lines.append("| " + " | ".join(_escape_table_cell(x) for x in row) + " |")
        return "\n".join(lines)

    def _cell_text(self, cell_block):
        """提取 table_cell 内子块的纯文本（拼接，忽略样式）。"""
        if not isinstance(cell_block, dict):
            return ""
        parts = []
        for child_id in cell_block.get("children") or []:
            child = self.blocks_by_id.get(child_id)
            if not isinstance(child, dict):
                continue
            ckey = BLOCK_TYPE_NAME.get(child.get("block_type"))
            if ckey:
                text_obj = child.get(ckey)
                if isinstance(text_obj, dict):
                    for elem in text_obj.get("elements", []):
                        run = elem.get("text_run") if isinstance(elem, dict) else None
                        if isinstance(run, dict):
                            parts.append(run.get("content", ""))
        return "".join(parts).strip()


# ===== 快照编排 =====

def fetch_doc_snapshot(client, doc_token, image_saver=None):
    """拉取文档并转换为 Markdown。返回 (title, markdown)。"""
    doc = client.get_docx_document(doc_token)
    title = (doc or {}).get("title", "") or "未命名文档"
    blocks = client.get_docx_blocks(doc_token)
    converter = DocxConverter(blocks, image_saver=image_saver)
    md = converter.convert()
    return title, md


def build_doc_markdown(title, original_url, md_content):
    """组装带头部说明的文档快照 Markdown。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    header = (f"# {title}\n\n"
              f"> 📄 飞书云文档快照 · [打开原文]({original_url}) · 缓存时间 {now}\n")
    return header + "\n" + (md_content or "[空文档]") + "\n"


def resolve_doc_token(client, link):
    """解析链接（docx / wiki）为最终 docx 文档 token。
    返回 doc_token；wiki 解析出的 obj_type 非 docx 时抛异常（暂不支持快照）。"""
    kind, token = link.get("kind"), link.get("token")
    if kind == URL_KIND_DOCX:
        return token
    if kind == URL_KIND_WIKI:
        node = client.get_wiki_node(token)
        obj_type = (node or {}).get("obj_type", "")
        obj_token = (node or {}).get("obj_token", "")
        if obj_type != "docx" or not obj_token:
            raise Exception(f"知识库节点类型为 {obj_type or '未知'}，暂仅支持 docx 文档快照")
        return obj_token
    # 旧版 doc（/docs/）API 不同，暂不支持
    raise Exception("旧版文档（doc）暂不支持快照，仅保留链接")


def cache_docs_for_messages(client, messages, save_doc, save_doc_image=None, on_progress=None):
    """扫描消息中的云文档链接并缓存快照。

    client: FeishuClient
    messages: 消息列表
    save_doc: callable(doc_token, title, md_content) -> rel_path（已存在则直接返回路径）
    save_doc_image: callable(image_token) -> rel_path 或 None（None 时图片留占位）
    on_progress: callable(done, total, message)

    返回 (doc_map, failures)：
    - doc_map: {消息中出现的 token -> 快照相对路径}（含 wiki token 映射）
    - failures: [(token, 错误说明)]
    """
    # 1. 按消息顺序收集去重链接
    links = []
    seen_tokens = set()
    for m in messages:
        for link in extract_doc_links(m):
            key = (link["kind"], link["token"])
            if key not in seen_tokens:
                seen_tokens.add(key)
                links.append(link)
    if not links:
        return {}, []

    # 2. 逐个解析并缓存
    doc_map = {}
    failures = []
    cache_images = getattr(Config, "CACHE_DOC_IMAGES", True)
    resolved = {}  # doc_token -> rel_path（同一文档被多条链接引用时复用）

    total = len(links)
    for idx, link in enumerate(links):
        token = link["token"]
        if on_progress:
            on_progress(idx, total, f"缓存云文档 ({idx + 1}/{total})...")
        try:
            doc_token = resolve_doc_token(client, link)

            if doc_token in resolved:
                doc_map[token] = resolved[doc_token]
                continue

            def _image_saver(image_token):
                if not cache_images or not save_doc_image:
                    return None
                try:
                    return save_doc_image(image_token)
                except Exception as e:
                    print(f"[doc_cache] 下载文档图片失败: {e}")
                    return None

            title, md = fetch_doc_snapshot(client, doc_token, image_saver=_image_saver)
            original_url = f"https://feishu.cn/{'wiki' if link['kind'] == 'wiki' else 'docx'}/{token}"
            content = build_doc_markdown(title, original_url, md)
            rel_path = save_doc(doc_token, title, content)
            resolved[doc_token] = rel_path
            doc_map[token] = rel_path
        except Exception as e:
            failures.append((token, str(e)))
            print(f"[doc_cache] 文档缓存失败 token={token}: {e}")

    if on_progress:
        on_progress(total, total, f"云文档缓存完成（{len(doc_map)}/{total}）")

    return doc_map, failures
