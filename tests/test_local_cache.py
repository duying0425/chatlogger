import os
import shutil
import tempfile
import unittest
import zipfile
import io
import json
from config import Config

# 使用独立临时数据库和临时缓存目录测试
temp_dir = tempfile.mkdtemp(prefix="chatlogger_test_")
Config.DB_PATH = os.path.join(temp_dir, "test_chatlogger.db")
Config.LOCAL_CACHE_DIR = os.path.join(temp_dir, "test_cache")

import models
import local_cache
from app import app


class LocalCacheTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        models.init_db()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(temp_dir, ignore_errors=True)

    def setUp(self):
        self.user = models.get_or_create_user("ou_test123", "测试用户")
        self.chat_id = "oc_test_chat_001"
        self.chat_name = "项目研发交流群"

    def test_01_models_local_cache(self):
        """测试数据库操作：local_cache 字段增删查改"""
        # 添加群聊，开启本地缓存
        models.add_chat(self.user["id"], self.chat_id, self.chat_name, local_cache=1)
        chat = models.get_chat(self.user["id"], self.chat_id)
        self.assertIsNotNone(chat)
        self.assertEqual(chat["local_cache"], 1)

        # 切换本地缓存为关闭
        models.update_chat_local_cache(self.user["id"], self.chat_id, False)
        chat = models.get_chat(self.user["id"], self.chat_id)
        self.assertEqual(chat["local_cache"], 0)

        # 再次切换为开启
        models.update_chat_local_cache(self.user["id"], self.chat_id, True)
        chat = models.get_chat(self.user["id"], self.chat_id)
        self.assertEqual(chat["local_cache"], 1)

        # 测试排序：新添加的群排在最前面
        chat_id_2 = "oc_test_chat_002"
        models.add_chat(self.user["id"], chat_id_2, "第二个测试群")
        chats = models.get_chats(self.user["id"])
        self.assertEqual(chats[0]["chat_id"], chat_id_2)

        # 当第一个群更新时间最新时，自动浮动到最前面 (updated_at DESC)
        conn = models.get_db()
        conn.execute("UPDATE chats SET updated_at = '2099-01-01 00:00:00' WHERE chat_id = ?", (self.chat_id,))
        conn.commit()
        conn.close()
        chats = models.get_chats(self.user["id"])
        self.assertEqual(chats[0]["chat_id"], self.chat_id)

    def test_02_sanitize_filename(self):
        """测试非法文件名清理（兼容 Windows/Linux）"""
        self.assertEqual(local_cache.sanitize_filename("test/file:name*?.txt"), "test_file_name_.txt")
        self.assertEqual(local_cache.sanitize_filename(""), "file")
        self.assertEqual(local_cache.sanitize_filename("..."), "file")

    def test_03_save_asset_and_deduplication(self):
        """测试附件落盘与重复文件命名"""
        content_a = b"fake image bytes A"
        path_a = local_cache.save_asset(self.chat_id, self.chat_name, content_a, "photo.jpg", "key_001")
        self.assertTrue(path_a.startswith("assets/"))
        self.assertTrue(path_a.endswith("photo.jpg"))

        # 同内容同名保存，应复用路径
        path_a2 = local_cache.save_asset(self.chat_id, self.chat_name, content_a, "photo.jpg", "key_001")
        self.assertEqual(path_a, path_a2)

        # 不同内容同名保存，应增加前缀区分
        content_b = b"fake image bytes B (different)"
        path_b = local_cache.save_asset(self.chat_id, self.chat_name, content_b, "photo.jpg", "key_002")
        self.assertNotEqual(path_a, path_b)
        self.assertTrue("key_002" in path_b or "photo.jpg" in path_b)

    def test_04_format_message_to_markdown(self):
        """测试各类消息格式化为标准 Markdown"""
        # 1. 纯文本消息
        msg_text = {
            "message_id": "om_001",
            "msg_type": "text",
            "body": {"content": json.dumps({"text": "大家早上好！"})},
            "create_time": "1773000000000"
        }
        md_text = local_cache.format_message_to_markdown(msg_text, "张三", "2026-09-09 10:00:00")
        self.assertIn("**张三** &nbsp; `2026-09-09 10:00:00`", md_text)
        self.assertIn("大家早上好！", md_text)
        self.assertTrue(md_text.endswith("---\n"))

        # 2. 图片消息
        msg_img = {
            "message_id": "om_002",
            "msg_type": "image",
            "body": {"content": json.dumps({"image_key": "img_001"})},
            "create_time": "1773000010000"
        }
        md_img = local_cache.format_message_to_markdown(
            msg_img, "李四", "2026-09-09 10:01:00",
            asset_map={"img_001": "assets/img_001.jpg"}
        )
        self.assertIn("![图片](assets/img_001.jpg)", md_img)

        # 3. 附件消息
        msg_file = {
            "message_id": "om_003",
            "msg_type": "file",
            "body": {"content": json.dumps({"file_key": "file_001", "file_name": "系统架构图.pdf"})},
            "create_time": "1773000020000"
        }
        md_file = local_cache.format_message_to_markdown(
            msg_file, "王五", "2026-09-09 10:02:00",
            asset_map={"file_001": "assets/系统架构图.pdf"}
        )
        self.assertIn("[📎 系统架构图.pdf](assets/系统架构图.pdf)", md_file)

        # 4. 富文本消息（含样式、链接、内嵌图片与代码块）
        post_content = {
            "zh_cn": {
                "title": "发布通知",
                "content": [
                    [
                        {"tag": "text", "text": "注意：", "style": ["bold"]},
                        {"tag": "text", "text": "版本已发布至 "},
                        {"tag": "a", "text": "官网地址", "href": "https://example.com"}
                    ],
                    [
                        {"tag": "img", "image_key": "img_post_01"}
                    ],
                    [
                        {"tag": "code_block", "text": "npm run start", "language": "bash"}
                    ]
                ]
            }
        }
        msg_post = {
            "message_id": "om_004",
            "msg_type": "post",
            "body": {"content": json.dumps(post_content)},
            "create_time": "1773000030000"
        }
        md_post = local_cache.format_message_to_markdown(
            msg_post, "赵六", "2026-09-09 10:03:00",
            asset_map={"img_post_01": "assets/img_post_01.jpg"}
        )
        self.assertIn("### 发布通知", md_post)
        self.assertIn("**注意：**", md_post)
        self.assertIn("[官网地址](https://example.com)", md_post)
        self.assertIn("![图片](assets/img_post_01.jpg)", md_post)
        self.assertIn("```bash\nnpm run start\n```", md_post)

    def test_05_append_and_build_zip(self):
        """测试追加消息入库、信息获取及打包 ZIP"""
        block1 = "**测试员A** &nbsp; `2026-09-09 11:00:00`\n\n消息1\n\n---\n"
        block2 = "**测试员B** &nbsp; `2026-09-09 11:01:00`\n\n消息2\n\n---\n"
        local_cache.append_messages_to_cache(self.chat_id, self.chat_name, [block1, block2])

        self.assertTrue(local_cache.has_cache(self.chat_id, self.chat_name))
        info = local_cache.get_cache_info(self.chat_id, self.chat_name)
        self.assertTrue(info["exists"])
        self.assertGreater(info["md_size"], 0)

        raw = local_cache.get_raw_markdown(self.chat_id, self.chat_name)
        self.assertIn("消息1", raw)
        self.assertIn("消息2", raw)

        # 测试打包 ZIP
        mem_file, zip_name = local_cache.build_cache_zip(self.chat_id, self.chat_name)
        self.assertTrue(zip_name.endswith(".zip"))
        with zipfile.ZipFile(mem_file, "r") as zf:
            namelist = zf.namelist()
            self.assertTrue(any(n.endswith(".md") for n in namelist))

    def test_06_flask_routes(self):
        """测试 Flask 相关路由：在线预览、原始Markdown下载、ZIP下载、附件安全访问"""
        client = app.test_client()

        # 模拟登录 session
        with client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        # 1. 测试切换开关 API
        resp = client.post(f"/api/chats/{self.chat_id}/toggle_cache", json={"enabled": False})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["local_cache"])

        resp = client.post(f"/api/chats/{self.chat_id}/toggle_cache", json={"enabled": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["local_cache"])

        # 2. 测试预览页面
        resp = client.get(f"/cache/{self.chat_id}/view")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn(self.chat_name, html)
        self.assertIn("marked.min.js", html)
        self.assertIn("lightboxModal", html)

        # 3. 测试原始 Markdown 路由
        resp = client.get(f"/cache/{self.chat_id}/raw")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/markdown", resp.headers.get("Content-Type", ""))
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))

        # 4. 测试 ZIP 下载路由
        resp = client.get(f"/cache/{self.chat_id}/download")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "application/zip")
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))

        # 5. 测试静态附件服务
        # 创建一个测试附件
        local_cache.save_asset(self.chat_id, self.chat_name, b"test-pdf-bytes", "report.pdf", "f001")
        resp = client.get(f"/cache/{self.chat_id}/assets/report.pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"test-pdf-bytes")
        resp.close()

        # 6. 测试防目录遍历安全性
        resp = client.get(f"/cache/{self.chat_id}/assets/../../config.py")
        self.assertIn(resp.status_code, (403, 404))
        resp.close()

    def test_07_sync_integration_mock(self):
        """测试从 FeishuClient mock 到 Markdown 写入和预览的完整同步流程"""
        from unittest.mock import MagicMock, patch
        import app as app_module

        # 设置用户 token
        models.update_user_tokens(self.user["id"], "mock_access_token", "mock_refresh_token", 7200, 7200)

        mock_client = MagicMock()
        mock_client.get_chat_info.return_value = {"name": self.chat_name}
        mock_client.get_chat_members_safe.return_value = {"ou_user_a": "张三(架构师)", "ou_user_b": "李四(前端)"}
        mock_client.list_all_messages.return_value = [
            {
                "message_id": "om_sync_01",
                "message_position": "1",
                "msg_type": "text",
                "create_time": "1773001000000",
                "sender": {"id": "ou_user_a"},
                "body": {"content": json.dumps({"text": "大家看下新版方案"})},
            },
            {
                "message_id": "om_sync_02",
                "message_position": "2",
                "msg_type": "image",
                "create_time": "1773001010000",
                "sender": {"id": "ou_user_b"},
                "body": {"content": json.dumps({"image_key": "img_sync_01"})},
            },
            {
                "message_id": "om_sync_03",
                "message_position": "3",
                "msg_type": "file",
                "create_time": "1773001020000",
                "sender": {"id": "ou_user_a"},
                "body": {"content": json.dumps({"file_key": "file_sync_01", "file_name": "API设计文档.docx"})},
            }
        ]
        mock_client.create_bitable.return_value = ("base_token_123", "table_id_123", "https://feishu.cn/base/xxx")
        mock_client.batch_create_records.return_value = ["rec_01", "rec_02", "rec_03"]
        mock_client.download_resource.side_effect = [
            (b"fake-image-bytes", "img_sync_01.jpg"),
            (b"fake-doc-bytes", "API设计文档.docx")
        ]
        mock_client.upload_file.return_value = "token_file_abc"
        mock_client.upload_attachment_to_record.return_value = True

        with patch("app.FeishuClient", return_value=mock_client):
            app_module._run_sync(self.user["id"], self.chat_id)

        # 验证同步进度已完成
        prog = app_module._get_progress(self.chat_id)
        self.assertEqual(prog["stage"], "done")
        self.assertTrue(prog["result"]["local_cache"])
        self.assertTrue(prog["result"]["has_cache"])

        # 验证本地文件生成
        raw = local_cache.get_raw_markdown(self.chat_id, self.chat_name)
        self.assertIn("张三(架构师)", raw)
        self.assertIn("大家看下新版方案", raw)
        self.assertIn("![图片](assets/img_sync_01.jpg)", raw)
        self.assertIn("API设计文档.docx", raw)

        # 验证 assets 文件落盘
        chat_dir = local_cache.get_chat_cache_dir(self.chat_id, self.chat_name)
        self.assertTrue(os.path.exists(os.path.join(chat_dir, "assets", "img_sync_01.jpg")))
        self.assertTrue(os.path.exists(os.path.join(chat_dir, "assets", "API设计文档.docx")))



if __name__ == "__main__":
    unittest.main()
