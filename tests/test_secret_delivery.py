"""密钥的**文件**投递方式（P2-8）。

环境变量投密钥的老问题：它会进 shell 历史、`docker inspect`、
`/proc/<pid>/environ`，而且轮换没有落点。文件方式（Docker secret /
K8s Secret 挂载都是这一个约定）把这两件事都解决掉。

这里钉住的是**失败形态**：给了路径却读不出来时，必须**拒绝启动**，
而不是退回默认值或空串。一个"我配了密钥但其实没生效"的服务，
比"配错就起不来"危险得多 —— 前者看起来一切正常。

⚠️ 为什么走环境变量而不是 ``Settings(_env_file=...)``：
pydantic 的 mypy 插件按**模型字段**合成 ``__init__`` 签名，
不认 pydantic-settings 那批 ``_env_file`` / ``_secrets_dir`` 下划线参数，
于是 ``Settings(_env_file=None)`` 运行时完全合法、在 mypy 下却是
``Unexpected keyword argument``。本项目的硬规矩是全仓 0 处 ``type: ignore``
（有了第一处就会有第二处，而且它会把真问题一起压掉），
所以这里用仓库既有的方式：``monkeypatch.setenv`` + ``reset_settings_cache``，
与 ``test_metrics.py`` / ``test_hardening.py`` 同一套写法。

另一个要点是**用例自己负责隔离 ``.env``**：``Settings`` 默认读 cwd 下的
``.env``，把它挪进空目录（``monkeypatch.chdir(tmp_path)``）之后，
本机开发者有没有 ``.env`` 都不影响断言 —— 否则就是那种
"我这台机器上跑不过"的最难排查的用例。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lagent.config import get_settings, reset_settings_cache


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    """每个用例都在"干净的配置世界"里跑。

    * ``monkeypatch.chdir(tmp_path)``：把 ``.env`` 的解析指向一个空目录；
    * ``reset_settings_cache()``（前后各一次）：``get_settings`` 是 lru_cache
      单例，上一个用例的缓存会把这一个用例的环境变量整个吞掉 ——
      这套用例量小，每次全量重建最便宜也最不容易出错。
    """
    monkeypatch.chdir(tmp_path)
    reset_settings_cache()
    yield
    reset_settings_cache()


class TestSecretFromFile:
    def test_the_file_content_wins(self, monkeypatch, tmp_path):
        path = tmp_path / "jwt"
        path.write_text("from-the-file\n", encoding="utf-8")
        monkeypatch.setenv("LAB_JWT_SECRET_FILE", str(path))
        reset_settings_cache()
        # 末尾换行必须被去掉：挂载进来的文件几乎都带换行，
        # 而多一个 \n 的密钥与不带的是**两个**密钥，签名互不相认。
        assert get_settings().jwt_secret == "from-the-file"

    def test_it_overrides_the_plain_env_value(self, monkeypatch, tmp_path):
        """两者都给时文件优先：显式指向文件的意图更强，
        env 那一份常常只是历史遗留。"""
        path = tmp_path / "jwt"
        path.write_text("from-the-file", encoding="utf-8")
        monkeypatch.setenv("LAB_JWT_SECRET", "from-env")
        monkeypatch.setenv("LAB_JWT_SECRET_FILE", str(path))
        reset_settings_cache()
        assert get_settings().jwt_secret == "from-the-file"

    def test_a_missing_file_refuses_to_start(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LAB_JWT_SECRET_FILE", str(tmp_path / "nope"))
        reset_settings_cache()
        with pytest.raises(ValidationError, match="JWT_SECRET_FILE"):
            get_settings()

    def test_an_empty_file_refuses_to_start(self, monkeypatch, tmp_path):
        path = tmp_path / "jwt"
        path.write_text("   \n", encoding="utf-8")
        monkeypatch.setenv("LAB_JWT_SECRET_FILE", str(path))
        reset_settings_cache()
        with pytest.raises(ValidationError, match="JWT_SECRET_FILE"):
            get_settings()

    def test_the_same_convention_works_for_the_mail_password(self, monkeypatch, tmp_path):
        path = tmp_path / "smtp"
        path.write_text("mail-secret", encoding="utf-8")
        monkeypatch.setenv("LAB_SMTP_PASSWORD_FILE", str(path))
        reset_settings_cache()
        assert get_settings().smtp_password == "mail-secret"

    def test_no_file_means_unchanged_behaviour(self, monkeypatch):
        """不给 *_FILE 时行为必须和以前一模一样 —— 这是纯加成改动，
        老部署不该因为升级就开始找文件。"""
        monkeypatch.setenv("LAB_JWT_SECRET", "plain-env-secret")
        reset_settings_cache()
        settings = get_settings()
        assert settings.jwt_secret == "plain-env-secret"
        assert settings.smtp_password == ""
