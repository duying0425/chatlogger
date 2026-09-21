import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import models
from feishu import check_feishu_chat_error
from app import app, _get_progress, _run_sync


class ChatExitedTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        models.init_db()

    def setUp(self):
        self.client = app.test_client()
        self.user = models.get_or_create_user("ou_exited_test_user_001", "退群测试用户")
        models.update_user_tokens(
            self.user["id"],
            access_token="test_access_token",
            refresh_token="test_refresh_token",
            expires_in=7200,
            refresh_expires_in=2592000,
        )

    def test_check_feishu_chat_error_helper(self):
        """测试 check_feishu_chat_error 各种特征错误识别"""
        # 1. 用户不在群中
        err1 = Exception("获取消息失败: {'code': 230002, 'msg': 'The user is not in the chat.'}")
        res1 = check_feishu_chat_error(err1)
        self.assertIsNotNone(res1)
        self.assertEqual(res1["type"], "not_in_chat")
        self.assertEqual(res1["badge_text"], "已退出群聊")

        err2 = Exception("The user is not in the chat")
        res2 = check_feishu_chat_error(err2)
        self.assertEqual(res2["type"], "not_in_chat")

        err3 = Exception("用户不在群聊中")
        res3 = check_feishu_chat_error(err3)
        self.assertEqual(res3["type"], "not_in_chat")

        err4 = Exception("{'code': 230020, 'msg': 'Bot or user is not in the chat'}")
        res4 = check_feishu_chat_error(err4)
        self.assertEqual(res4["type"], "not_in_chat")

        # 2. 群已解散
        err_dissolved = Exception("{'code': 230005, 'msg': 'The chat has been dissolved'}")
        res_dissolved = check_feishu_chat_error(err_dissolved)
        self.assertIsNotNone(res_dissolved)
        self.assertEqual(res_dissolved["type"], "dissolved")
        self.assertEqual(res_dissolved["badge_text"], "群已解散")

        # 3. 群不存在
        err_not_found = Exception("{'code': 230001, 'msg': 'Chat not found'}")
        res_not_found = check_feishu_chat_error(err_not_found)
        self.assertIsNotNone(res_not_found)
        self.assertEqual(res_not_found["type"], "not_found")
        self.assertEqual(res_not_found["badge_text"], "群不存在")

        # 4. 普通网络或限流错误
        err_other = Exception("Connection refused by peer")
        self.assertIsNone(check_feishu_chat_error(err_other))

    @patch("app.get_feishu_client")
    def test_api_chat_stats_not_in_chat(self, mock_get_client):
        """当用户已退群时，/api/chat_stats 应优雅返回 200 及 not_in_chat 状态，而非 500 报错"""
        chat_id = "oc_exited_stats_001"
        models.add_chat(self.user["id"], chat_id, chat_name="已退出的群聊")
        models.update_chat_sync_status(self.user["id"], chat_id, last_position=50, record_count=50)

        mock_client = MagicMock()
        mock_client.get_chat_latest_meta.side_effect = Exception("获取消息失败: {'code': 230002, 'msg': 'The user is not in the chat.'}")
        mock_get_client.return_value = mock_client

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        resp = self.client.get(f"/api/chat_stats/{chat_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertIsNone(data.get("error"))
        self.assertEqual(data.get("chat_error"), "not_in_chat")
        self.assertEqual(data.get("badge_text"), "已退出群聊")
        self.assertIn("不在该群聊中", data.get("chat_error_msg", ""))
        self.assertEqual(data.get("synced"), 50)
        self.assertEqual(data.get("pending"), 0)

    @patch("app.get_feishu_client")
    def test_api_chat_stats_dissolved(self, mock_get_client):
        """当群聊解散时，/api/chat_stats 应优雅返回 200 及 dissolved 状态"""
        chat_id = "oc_dissolved_stats_002"
        models.add_chat(self.user["id"], chat_id, chat_name="已解散的群聊")

        mock_client = MagicMock()
        mock_client.get_chat_latest_meta.side_effect = Exception("获取消息失败: {'code': 230005, 'msg': 'The chat has been dissolved'}")
        mock_get_client.return_value = mock_client

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        resp = self.client.get(f"/api/chat_stats/{chat_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("chat_error"), "dissolved")
        self.assertEqual(data.get("badge_text"), "群已解散")

    @patch("app.get_feishu_client")
    def test_fetch_name_when_not_in_chat(self, mock_get_client):
        """预拉取群名时若不在群中，给出直观明确的 warning"""
        mock_client = MagicMock()
        mock_client.get_chat_info.side_effect = Exception("获取群信息失败: {'code': 230002, 'msg': 'The user is not in the chat.'}")
        mock_get_client.return_value = mock_client

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        resp = self.client.get("/api/chats/fetch_name?chat_id=oc_not_in_chat")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data.get("ok"))
        self.assertIn("不在该群聊中", data.get("warning", ""))

    @patch("app.FeishuClient")
    def test_run_sync_when_not_in_chat(self, mock_client_cls):
        """测试后台同步任务遇到 230002 错误时，进度状态包含清晰提示与 error_type='not_in_chat'"""
        chat_id = "oc_sync_exited_003"
        models.add_chat(self.user["id"], chat_id, chat_name="退群同步测试")

        mock_client = MagicMock()
        mock_client.get_chat_info.return_value = {"name": "退群同步测试"}
        mock_client.list_all_messages.side_effect = Exception("获取消息失败: {'code': 230002, 'msg': 'The user is not in the chat.'}")
        mock_client_cls.return_value = mock_client

        # 直接调用同步流程
        _run_sync(self.user["id"], chat_id)

        p = _get_progress(chat_id)
        self.assertIsNotNone(p)
        self.assertFalse(p.get("running"))
        self.assertEqual(p.get("stage"), "error")
        self.assertEqual(p.get("error_type"), "not_in_chat")
        self.assertIn("不在该群聊中", p.get("error", ""))
        self.assertIn("已有本地归档不受影响", p.get("error", ""))


if __name__ == "__main__":
    unittest.main()

