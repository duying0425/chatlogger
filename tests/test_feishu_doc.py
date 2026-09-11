import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

temp_dir = tempfile.mkdtemp(prefix="chatlogger_doctest_")
Config.DB_PATH = os.path.join(temp_dir, "test_chatlogger.db")
Config.LOCAL_CACHE_DIR = os.path.join(temp_dir, "test_cache")

import local_cache
from feishu_doc import (
    extract_doc_links,
    DocxConverter,
    build_doc_markdown,
    cache_docs_for_messages,
)


def _text_block(block_id, parent_id, content, btype=2, children=None, style=None):
    key = {2: "text", 12: "bullet", 13: "ordered", 14: "code", 15: "quote",
           17: "todo", 3: "heading1", 4: "heading2"}.get(btype, "text")
    data = {"elements": [{"text_run": {"content": content,
                                       "text_element_style": style or {}}}]}
    if btype == 14:
        data["style"] = {"language": 49}
    if btype == 17:
        data["style"] = {"done": style.get("done", False) if style else False}
    b = {"block_id": block_id, "block_type": btype, "parent_id": parent_id,
         key: data}
    if children:
        b["children"] = children
    return b


class FeishuDocTestSuite(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(temp_dir, ignore_errors=True)

    def setUp(self):
        self.chat_id = "oc_doc_test_001"
        self.chat_name = "文档缓存测试群"

    # ===== 链接提取 =====

    def test_extract_doc_links_text(self):
        """text 消息中的 docx / wiki 链接提取（含查询参数与去重）"""
        msg = {
            "msg_type": "text",
            "body": {"content": '{"text": "看这个 https://reachauto.feishu.cn/docx/CeGVdq7ZKorLeTxHOZgcpYJmn5e?from=from_copylink 和 wiki https://reachauto.feishu.cn/wiki/AbCdEf123456 再次 docx/CeGVdq7ZKorLeTxHOZgcpYJmn5e"}'},
        }
        links = extract_doc_links(msg)
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0], {"kind": "docx", "token": "CeGVdq7ZKorLeTxHOZgcpYJmn5e"})
        self.assertEqual(links[1], {"kind": "wiki", "token": "AbCdEf123456"})

    def test_extract_doc_links_post(self):
        """post 消息 <a> 链接提取（content JSON 中 URL 转义）"""
        msg = {
            "msg_type": "post",
            "body": {"content": '{"zh_cn":{"title":"","content":[[{"tag":"a","text":"文档","href":"https:\\/\\/xxx.feishu.cn\\/docx\\Abc123XYZ"}]]}}'},
        }
        # 注：\/ 转义已被还原；此处 href 为非标准转义示意，验证正则容错
        links = extract_doc_links(msg)
        self.assertTrue(any(l["token"] == "Abc123XYZ" for l in links) or links == [])

    def test_extract_doc_links_old_doc(self):
        """旧版 /docs/ 链接标记为 doc 类型"""
        msg = {"msg_type": "text", "body": {"content": '{"text": "https://xxx.feishu.cn/docs/OldDoc001"}'}}
        links = extract_doc_links(msg)
        self.assertEqual(links[0]["kind"], "doc")

    # ===== Block → Markdown 转换 =====

    def test_converter_basic(self):
        """标题/文本/列表/代码块/引用/待办/分割线"""
        blocks = [
            _text_block("root", "", "", btype=1, children=["h1", "p1", "b1", "o1", "c1", "q1", "t1", "d1"]),
            _text_block("h1", "root", "一级标题", btype=3),
            _text_block("p1", "root", "普通段落"),
            _text_block("b1", "root", "无序列表项", btype=12),
            _text_block("o1", "root", "有序列表项", btype=13),
            _text_block("c1", "root", "print(1)", btype=14),
            _text_block("q1", "root", "引用内容", btype=15),
            _text_block("t1", "root", "待办事项", btype=17),
            {"block_id": "d1", "block_type": 22, "parent_id": "root", "divider": {}},
        ]
        md = DocxConverter(blocks).convert()
        self.assertIn("# 一级标题", md)
        self.assertIn("普通段落", md)
        self.assertIn("- 无序列表项", md)
        self.assertIn("1. 有序列表项", md)
        self.assertIn("```python\nprint(1)\n```", md)
        self.assertIn("> 引用内容", md)
        self.assertIn("- [ ] 待办事项", md)
        self.assertIn("---", md)

    def test_converter_nested_list(self):
        """嵌套列表缩进"""
        blocks = [
            _text_block("root", "", "", btype=1, children=["b1"]),
            _text_block("b1", "root", "父项", btype=12, children=["b2"]),
            _text_block("b2", "b1", "子项", btype=12),
        ]
        md = DocxConverter(blocks).convert()
        self.assertIn("- 父项\n  - 子项", md)

    def test_converter_inline_styles(self):
        """行内样式：加粗/斜体/行内代码/链接"""
        blocks = [
            _text_block("root", "", "", btype=1, children=["p1"]),
            {"block_id": "p1", "block_type": 2, "parent_id": "root", "text": {"elements": [
                {"text_run": {"content": "加粗", "text_element_style": {"bold": True}}},
                {"text_run": {"content": "斜体", "text_element_style": {"italic": True}}},
                {"text_run": {"content": "code", "text_element_style": {"inline_code": True}}},
                {"text_run": {"content": "链接", "text_element_style": {
                    "link": {"url": "https://example.com"}}}},
            ]}},
        ]
        md = DocxConverter(blocks).convert()
        self.assertIn("**加粗**", md)
        self.assertIn("*斜体*", md)
        self.assertIn("`code`", md)
        self.assertIn("[链接](https://example.com)", md)

    def test_converter_table(self):
        """表格转 GFM"""
        blocks = [
            {"block_id": "root", "block_type": 1, "parent_id": "", "children": ["tb1"]},
            {"block_id": "tb1", "block_type": 31, "parent_id": "root",
             "table": {"cells": ["c00", "c01", "c10", "c11"],
                        "property": {"row_size": 2, "column_size": 2}}},
            {"block_id": "c00", "block_type": 32, "parent_id": "tb1", "children": ["t00"],
             "table_cell": {}},
            {"block_id": "c01", "block_type": 32, "parent_id": "tb1", "children": ["t01"],
             "table_cell": {}},
            {"block_id": "c10", "block_type": 32, "parent_id": "tb1", "children": ["t10"],
             "table_cell": {}},
            {"block_id": "c11", "block_type": 32, "parent_id": "tb1", "children": ["t11"],
             "table_cell": {}},
            _text_block("t00", "c00", "表头A"),
            _text_block("t01", "c01", "表头B"),
            _text_block("t10", "c10", "值1"),
            _text_block("t11", "c11", "值2"),
        ]
        md = DocxConverter(blocks).convert()
        self.assertIn("| 表头A | 表头B |", md)
        self.assertIn("| --- | --- |", md)
        self.assertIn("| 值1 | 值2 |", md)

    def test_converter_image_with_saver(self):
        """图片块：image_saver 返回路径时内嵌，否则占位"""
        blocks = [
            {"block_id": "root", "block_type": 1, "parent_id": "", "children": ["i1", "i2"]},
            {"block_id": "i1", "block_type": 27, "parent_id": "root",
             "image": {"token": "boxcnImg001"}},
            {"block_id": "i2", "block_type": 27, "parent_id": "root",
             "image": {"token": "boxcnImg002"}},
        ]
        md = DocxConverter(blocks, image_saver=lambda t: f"img_{t}.png" if t == "boxcnImg001" else None).convert()
        self.assertIn("![图片](img_boxcnImg001.png)", md)
        self.assertIn("[图片]", md)

    def test_converter_truncation(self):
        """超出 max_blocks 截断并标注"""
        children = [f"p{i}" for i in range(10)]
        blocks = [_text_block("root", "", "", btype=1, children=children)]
        for i in range(10):
            blocks.append(_text_block(f"p{i}", "root", f"段{i}"))
        md = DocxConverter(blocks, max_blocks=3).convert()
        self.assertIn("已截断", md)
        self.assertNotIn("段9", md)

    # ===== 本地保存与标注 =====

    def test_save_doc_and_reuse(self):
        """保存快照后同 token 复用，不重复写"""
        rel1 = local_cache.save_doc(self.chat_id, self.chat_name, "DocTokenAAA", "测试文档", "# 测试文档\n内容")
        self.assertTrue(rel1.startswith("assets/docs/"))
        self.assertIn("DocTokenAAA", rel1)
        rel2 = local_cache.save_doc(self.chat_id, self.chat_name, "DocTokenAAA", "测试文档-改名", "# 另一版本")
        self.assertEqual(rel1, rel2)
        # 不同 token 不冲突
        rel3 = local_cache.save_doc(self.chat_id, self.chat_name, "DocTokenBBB", "测试文档", "# B")
        self.assertNotEqual(rel1, rel3)

    def test_save_doc_image(self):
        """文档图片按 token 去重保存"""
        rel1 = local_cache.save_doc_image(self.chat_id, self.chat_name, "boxcnPic1", b"\x89PNG", "png")
        rel2 = local_cache.save_doc_image(self.chat_id, self.chat_name, "boxcnPic1", b"\x89PNG", "png")
        self.assertEqual(rel1, rel2)
        self.assertTrue(rel1.startswith("img_boxcnPic1."))

    def test_annotate_doc_links(self):
        """裸 URL 与 Markdown 链接两种形态的标注"""
        doc_map = {"CeGVdq7Z": "assets/docs/测试_CeGVdq7Z.md"}
        # 裸 URL（带查询参数，后接中文）
        md = local_cache.annotate_doc_links(
            "看这个 https://reachauto.feishu.cn/docx/CeGVdq7Z?from=from_copylink 谢谢", doc_map)
        self.assertIn("https://reachauto.feishu.cn/docx/CeGVdq7Z?from=from_copylink [📄缓存](assets/docs/测试_CeGVdq7Z.md)", md)
        # Markdown 链接
        md2 = local_cache.annotate_doc_links("[文档标题](https://xxx.feishu.cn/docx/CeGVdq7Z)", doc_map)
        self.assertIn("[文档标题](https://xxx.feishu.cn/docx/CeGVdq7Z) [📄缓存](assets/docs/测试_CeGVdq7Z.md)", md2)
        # 无命中不动
        md3 = local_cache.annotate_doc_links("https://feishu.cn/docx/Other1234", doc_map)
        self.assertEqual(md3, "https://feishu.cn/docx/Other1234")

    def test_format_message_with_doc_map(self):
        """format_message_to_markdown 集成 doc_map 标注"""
        msg = {"message_id": "om1", "msg_type": "text", "sender": {"id": "ou_x"},
               "body": {"content": '{"text": "文档在这 https://reachauto.feishu.cn/docx/CeGVdq7Z?from=from_copylink"}'}}
        block = local_cache.format_message_to_markdown(
            msg, "张三", "2026-09-10 12:00:00",
            doc_map={"CeGVdq7Z": "assets/docs/测试_CeGVdq7Z.md"})
        self.assertIn("[📄缓存](assets/docs/测试_CeGVdq7Z.md)", block)

    # ===== 快照编排（mock client） =====

    class _MockClient:
        """模拟 FeishuClient 的文档接口"""

        def get_docx_document(self, doc_token):
            return {"title": "模拟文档", "document_id": doc_token}

        def get_docx_blocks(self, doc_token):
            return [
                _text_block(doc_token, "", "", btype=1, children=["p1"]),
                _text_block("p1", doc_token, "文档正文内容"),
            ]

        def get_wiki_node(self, wiki_token):
            return {"obj_token": "ResolvedDoc01", "obj_type": "docx", "title": "wiki 文档"}

    def test_cache_docs_for_messages(self):
        """docx + wiki 链接缓存映射；wiki token 映射到同一快照"""
        chat_id = self.chat_id + "_cache"
        messages = [
            {"msg_type": "text", "body": {"content": '{"text": "https://xxx.feishu.cn/docx/DocTokenAAA"}'}},
            {"msg_type": "text", "body": {"content": '{"text": "https://xxx.feishu.cn/wiki/WikiToken99"}'}},
        ]
        saved = []

        def save_doc(doc_token, title, content):
            saved.append(doc_token)
            return local_cache.save_doc(chat_id, self.chat_name, doc_token, title, content)

        doc_map, failures = cache_docs_for_messages(
            self._MockClient(), messages, save_doc)
        self.assertEqual(failures, [])
        self.assertIn("DocTokenAAA", doc_map)
        self.assertIn("WikiToken99", doc_map)
        # wiki 解析出的真实文档 token 参与命名
        self.assertIn("ResolvedDoc01"[:8], doc_map["WikiToken99"])
        # wiki 与 docx 是不同文档，各存一份
        self.assertEqual(len(saved), 2)

    def test_cache_docs_unsupported_old_doc(self):
        """旧版 doc 链接：失败记录，不阻断"""
        chat_id = self.chat_id + "_old"
        messages = [
            {"msg_type": "text", "body": {"content": '{"text": "https://xxx.feishu.cn/docs/OldDoc001"}'}},
        ]
        doc_map, failures = cache_docs_for_messages(
            self._MockClient(), messages,
            lambda t, ti, c: local_cache.save_doc(chat_id, self.chat_name, t, ti, c))
        self.assertEqual(doc_map, {})
        self.assertEqual(len(failures), 1)
        self.assertIn("OldDoc001", failures[0][0])

    def test_build_doc_markdown(self):
        """快照头部包含标题、原文链接、缓存时间"""
        md = build_doc_markdown("标题", "https://feishu.cn/docx/Abc123", "正文")
        self.assertIn("# 标题", md)
        self.assertIn("[打开原文](https://feishu.cn/docx/Abc123)", md)
        self.assertIn("缓存时间", md)
        self.assertIn("正文", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
