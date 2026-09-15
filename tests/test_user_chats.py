import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import models
from app import app
from feishu import FeishuClient


class UserChatsTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        models.init_db()

    def setUp(self):
        self.client = app.test_client()
        self.user = models.get_or_create_user("ou_user_chats_test_001", "群聊测试用户")
        models.update_user_tokens(
            self.user["id"],
            access_token="test_access_token_123",
            refresh_token="test_refresh_token_123",
            expires_in=7200,
            refresh_expires_in=2592000,
        )
        # 清理可能存在的旧数据
        conn = models.get_db()
        conn.execute("DELETE FROM chats WHERE user_id = ?", (self.user["id"],))
        conn.execute("DELETE FROM user_chats_cache WHERE user_id = ?", (self.user["id"],))
        conn.commit()
        conn.close()

    def tearDown(self):
        conn = models.get_db()
        conn.execute("DELETE FROM chats WHERE user_id = ?", (self.user["id"],))
        conn.execute("DELETE FROM user_chats_cache WHERE user_id = ?", (self.user["id"],))
        conn.commit()
        conn.close()

    # ===== Models 测试 =====

    def test_models_cache_crud(self):
        """测试 models 中 user_chats_cache 的写入、读取与 is_added 状态标记"""
        # 添加一个已配置群聊
        models.add_chat(self.user["id"], "oc_added_001", "已归档群A")

        sample_chats = [
            {"chat_id": "oc_added_001", "name": "已归档群A", "avatar": "http://img/1.png", "description": "群A描述"},
            {"chat_id": "oc_other_002", "name": "技术研发核心组", "avatar": "http://img/2.png", "description": "技术交流"},
            {"chat_id": "oc_other_003", "name": "运营推广协同群", "avatar": "", "description": "运营交流"},
        ]
        models.save_user_chats_cache(self.user["id"], sample_chats)

        chats = models.get_user_chats_cache(self.user["id"])
        self.assertEqual(len(chats), 3)

        # 校验 is_added 标记
        added_chat = next(c for c in chats if c["chat_id"] == "oc_added_001")
        self.assertTrue(added_chat["is_added"])
        other_chat = next(c for c in chats if c["chat_id"] == "oc_other_002")
        self.assertFalse(other_chat["is_added"])

        meta = models.get_user_chats_cache_last_updated(self.user["id"])
        self.assertEqual(meta["count"], 3)
        self.assertIsNotNone(meta["last_updated"])

    def test_models_cache_search(self):
        """测试按群全称、部分关键词、chat_id 进行搜索及排序权重"""
        sample_chats = [
            {"chat_id": "oc_alpha_111", "name": "项目研发沟通群"},
            {"chat_id": "oc_beta_222", "name": "研发效率工具讨论"},
            {"chat_id": "oc_gamma_333", "name": "全公司大群"},
        ]
        models.save_user_chats_cache(self.user["id"], sample_chats)

        # 关键词“研发”：应匹配前两个群
        res = models.search_user_chats_cache(self.user["id"], "研发")
        self.assertEqual(len(res), 2)
        chat_ids = [r["chat_id"] for r in res]
        self.assertIn("oc_alpha_111", chat_ids)
        self.assertIn("oc_beta_222", chat_ids)

        # 关键词按 chat_id 搜索
        res_id = models.search_user_chats_cache(self.user["id"], "gamma")
        self.assertEqual(len(res_id), 1)
        self.assertEqual(res_id[0]["chat_id"], "oc_gamma_333")

    # ===== FeishuClient 测试 =====

    @patch.object(FeishuClient, "_api_get")
    def test_feishu_list_user_chats(self, mock_api_get):
        """测试 FeishuClient.list_user_chats 的分页与解散群过滤"""
        client = FeishuClient(
            access_token="tok", refresh_token="ref",
            token_expires_at=9999999999, refresh_expires_at=9999999999,
            user_id=self.user["id"]
        )

        # 模拟两页返回：第一页有 has_more，第二页结束，其中包含一个已解散群
        # 模拟两页返回：包含正常群、彻底解散群 (dissolved)、解散并保留历史群 (dissolved_save)
        mock_api_get.side_effect = [
            {
                "code": 0,
                "data": {
                    "has_more": True,
                    "page_token": "page_token_2",
                    "items": [
                        {"chat_id": "oc_p1_1", "name": "群1", "chat_status": "normal"},
                        {"chat_id": "oc_p1_2", "name": "已解散群", "chat_status": "dissolved"},
                        {"chat_id": "oc_p1_2", "name": "彻底解散群", "chat_status": "dissolved"},
                        {"chat_id": "oc_p1_3", "name": "解散保留群", "chat_status": "dissolved_save"},
                    ]
                }
            },
            {
                "code": 0,
                "data": {
                    "has_more": False,
                    "items": [
                        {"chat_id": "oc_p2_1", "name": "群2", "chat_status": "normal"}
                    ]
                }
            }
        ]

        chats = client.list_user_chats(page_size=100)
        # 彻底解散的 oc_p1_2 应被过滤，解散保留历史的 oc_p1_3 应予以保留
        self.assertEqual(len(chats), 3)
        chat_ids = [c["chat_id"] for c in chats]
        self.assertIn("oc_p1_1", chat_ids)
        self.assertIn("oc_p1_3", chat_ids)
        self.assertIn("oc_p2_1", chat_ids)
        self.assertNotIn("oc_p1_2", chat_ids)
        self.assertEqual(mock_api_get.call_count, 2)

    # ===== API 路由测试 =====

    def test_api_user_chats_unauthorized(self):
        """未登录时获取或同步群聊缓存应返回 401"""
        resp = self.client.get("/api/user_chats")
        self.assertEqual(resp.status_code, 401)

        resp = self.client.post("/api/user_chats/sync")
        self.assertEqual(resp.status_code, 401)

    def test_api_get_user_chats_logged_in(self):
        """已登录时获取缓存群聊列表"""
        sample_chats = [{"chat_id": "oc_test_100", "name": "测试交流群"}]
        models.save_user_chats_cache(self.user["id"], sample_chats)

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        resp = self.client.get("/api/user_chats")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("count"), 1)
        self.assertEqual(data["chats"][0]["name"], "测试交流群")

    @patch("app.get_feishu_client")
    def test_api_sync_user_chats_success(self, mock_get_client):
        """已登录时手动触发同步飞书群聊缓存"""
        mock_feishu = MagicMock()
        mock_feishu.list_user_chats.return_value = [
            {"chat_id": "oc_sync_001", "name": "云端同步群A", "avatar": ""},
            {"chat_id": "oc_sync_002", "name": "云端同步群B", "avatar": ""}
        ]
        mock_get_client.return_value = mock_feishu

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        resp = self.client.post("/api/user_chats/sync")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("count"), 2)

        # 验证数据库是否已存入
        in_db = models.get_user_chats_cache(self.user["id"])
        self.assertEqual(len(in_db), 2)

    @patch("app.get_feishu_client")
    def test_api_sync_user_chats_bot_error(self, mock_get_client):
        """当应用未开通机器人能力时返回友好警告"""
        mock_feishu = MagicMock()
        mock_feishu.list_user_chats.side_effect = Exception("error code 232025: bot ability is not activated")
        mock_get_client.return_value = mock_feishu

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        resp = self.client.post("/api/user_chats/sync")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data.get("ok"))
        self.assertIn("应用未开通机器人能力", data.get("warning", ""))

    # ===== 添加群聊兼容性测试 =====

    @patch("app.get_feishu_client")
    def test_add_chat_by_chat_id(self, mock_get_client):
        """直接输入 oc_ 开头的 chat_id 添加群聊"""
        mock_feishu = MagicMock()
        mock_feishu.get_chat_info.return_value = {"name": "真实ID群"}
        mock_get_client.return_value = mock_feishu

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        resp = self.client.post("/api/chats", json={"chat_id": "oc_direct_999"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("chat_id"), "oc_direct_999")
        self.assertEqual(data.get("chat_name"), "真实ID群")

    @patch("app.get_feishu_client")
    def test_add_chat_by_full_group_name(self, mock_get_client):
        """输入已缓存的群聊全称添加群聊"""
        models.save_user_chats_cache(self.user["id"], [
            {"chat_id": "oc_from_cache_1", "name": "全景智能监控群"}
        ])

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        resp = self.client.post("/api/chats", json={"chat_id": "全景智能监控群"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("chat_id"), "oc_from_cache_1")
        self.assertEqual(data.get("chat_name"), "全景智能监控群")

    @patch("app.get_feishu_client")
    def test_add_chat_by_partial_group_name(self, mock_get_client):
        """输入部分群聊名称添加群聊（唯一匹配）"""
        models.save_user_chats_cache(self.user["id"], [
            {"chat_id": "oc_from_cache_2", "name": "后端架构设计组"}
        ])

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        resp = self.client.post("/api/chats", json={"chat_id": "架构设计"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("chat_id"), "oc_from_cache_2")
        self.assertEqual(data.get("chat_name"), "后端架构设计组")

    def test_add_chat_ambiguous_partial_name(self):
        """输入部分群聊名称匹配到多个群时，返回友好错误提示"""
        models.save_user_chats_cache(self.user["id"], [
            {"chat_id": "oc_dup_1", "name": "设计沟通群A"},
            {"chat_id": "oc_dup_2", "name": "设计沟通群B"}
        ])

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        resp = self.client.post("/api/chats", json={"chat_id": "设计沟通"})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertIn("匹配到多个群聊", data.get("error", ""))

    def test_add_chat_not_found(self):
        """输入不存在的群聊名称且不以 oc_ 开头时，返回友好 404 引导提示"""
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        resp = self.client.post("/api/chats", json={"chat_id": "不存在的群聊名字"})
        self.assertEqual(resp.status_code, 404)
        data = resp.get_json()
        self.assertIn("未在缓存中找到", data.get("error", ""))

    # ===== 全部开始同步 (/api/sync_all) 测试 =====

    def test_sync_all_unauthorized(self):
        """未登录调用全部开始同步接口返回 401"""
        resp = self.client.post("/api/sync_all")
        self.assertEqual(resp.status_code, 401)
        data = resp.get_json()
        self.assertIn("未登录", data.get("error", ""))

    @patch("app.get_feishu_client")
    def test_sync_all_no_chats(self, mock_client):
        """没有配置任何群聊时，返回 400 提示"""
        mock_client.return_value = MagicMock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        resp = self.client.post("/api/sync_all")
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertIn("暂无可同步", data.get("error", ""))

    @patch("app._run_sync")
    @patch("app.get_feishu_client")
    def test_sync_all_success_and_skips_running(self, mock_client, mock_run_sync):
        """测试全部开始同步批量启动及对已在运行任务的自动跳过"""
        import app as app_module
        mock_client.return_value = MagicMock()
        models.add_chat(self.user["id"], "oc_batch_1", "群聊1")
        models.add_chat(self.user["id"], "oc_batch_2", "群聊2")
        models.add_chat(self.user["id"], "oc_batch_3", "群聊3")

        # 模拟 oc_batch_2 已在运行中
        app_module._set_progress("oc_batch_2", running=True, stage="fetching_messages")

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        try:
            resp = self.client.post("/api/sync_all")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("total"), 3)
            self.assertEqual(len(data.get("started", [])), 2)
            self.assertIn("oc_batch_1", data.get("started"))
            self.assertIn("oc_batch_3", data.get("started"))
            self.assertEqual(data.get("skipped"), ["oc_batch_2"])
            self.assertIn("已启动 2 个群聊", data.get("message"))
            self.assertIn("已启动 2 个", data.get("message"))
            self.assertIn("1 个群聊已在同步中", data.get("message"))
        finally:
            # 清理 progress
            app_module._set_progress("oc_batch_1", stage="idle", running=False)
            app_module._set_progress("oc_batch_2", stage="idle", running=False)
            app_module._set_progress("oc_batch_3", stage="idle", running=False)

    @patch("app._run_sync")
    @patch("app.get_feishu_client")
    def test_sync_all_with_specified_chat_ids(self, mock_client, mock_run_sync):
        """测试全部开始同步仅同步指定的 chat_ids（智能跳过已同步满的群聊）"""
        import app as app_module
        mock_client.return_value = MagicMock()
        models.add_chat(self.user["id"], "oc_filter_1", "群聊1")
        models.add_chat(self.user["id"], "oc_filter_2", "群聊2")

        with self.client.session_transaction() as sess:
            sess["user_id"] = self.user["id"]

        try:
            # 只指定同步 oc_filter_2
            resp = self.client.post("/api/sync_all", json={"chat_ids": ["oc_filter_2"]})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("started"), ["oc_filter_2"])
            self.assertEqual(data.get("total"), 2)
            self.assertEqual(data.get("target_count"), 1)
        finally:
            app_module._set_progress("oc_filter_1", stage="idle", running=False)
            app_module._set_progress("oc_filter_2", stage="idle", running=False)

