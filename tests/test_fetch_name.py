import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import models
from app import app


class FetchNameTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        models.init_db()

    def setUp(self):
        self.client = app.test_client()
        self.user = models.get_or_create_user("ou_fetch_user_001", "拉取测试用户")
        # 预设用户 token 避免 get_feishu_client 为空
        models.update_user_tokens(
            self.user["id"],
            access_token="test_access_token",
            refresh_token="test_refresh_token",
            expires_in=7200,
            refresh_expires_in=2592000,
        )

    def test_unauthorized(self):
        """未登录时应返回 401"""
        resp = self.client.get("/api/chats/fetch_name?chat_id=oc_123")
        self.assertEqual(resp.status_code, 401)
        data = resp.get_json()
        self.assertEqual(data.get("error"), "未登录")

    def test_missing_chat_id(self):
        """缺少 chat_id 应返回 400"""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]
        resp = self.client.get("/api/chats/fetch_name")
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertEqual(data.get("error"), "请输入群聊 ID")

    @patch("app.get_feishu_client")
    def test_fetch_name_success(self, mock_get_client):
        """正常获取群名称"""
        mock_client = MagicMock()
        mock_client.get_chat_info.return_value = {"name": "飞书测试群123"}
        mock_get_client.return_value = mock_client

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        resp = self.client.get("/api/chats/fetch_name?chat_id=oc_success_001")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("chat_name"), "飞书测试群123")
        mock_client.get_chat_info.assert_called_once_with("oc_success_001")

    @patch("app.get_feishu_client")
    def test_fetch_name_permission_error(self, mock_get_client):
        """当飞书返回未开通机器人能力（232025）时，应返回友好警告"""
        mock_client = MagicMock()
        mock_client.get_chat_info.side_effect = Exception("error code 232025: app has no bot capability")
        mock_get_client.return_value = mock_client

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        resp = self.client.get("/api/chats/fetch_name?chat_id=oc_error_001")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data.get("ok"))
        self.assertIn("应用未开通机器人能力", data.get("warning", ""))

