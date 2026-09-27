"""密钥的**文件**投递方式（P2-8）。

环境变量投密钥的老问题：它会进 shell 历史、`docker inspect`、
`/proc/<pid>/environ`，而且轮换没有落点。文件方式（Docker secret /
K8s Secret 挂载都是这一个约定）把这两件事都解决掉。

这里钉住的是**失败形态**：给了路径却读不出来时，必须**拒绝启动**，
而不是退回默认值或空串。一个"我配了密钥但其实没生效"的服务，
比"配错就起不来"危险得多 —— 前者看起来一切正常。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lagent.config import Settings


def make_settings(tmp_path, **overrides) -> Settings:
    # _env_file=None：这些用例要的是"我显式给了什么"，不该被仓库根目录的
    # .env 悄悄改掉（不同机器上跑出不同结果的测试是最难排查的那种）。
    return Settings(_env_file=None, **overrides)


class TestSecretFromFile:
    def test_the_file_content_wins(self, tmp_path):
        path = tmp_path / "jwt"
        path.write_text("from-the-file\n", encoding="utf-8")
        settings = make_settings(tmp_path, jwt_secret_file=str(path))
        # 末尾换行必须被去掉：挂载进来的文件几乎都带换行，
        # 而多一个 \n 的密钥与不带的是**两个**密钥，签名互不相认。
        assert settings.jwt_secret == "from-the-file"

    def test_it_overrides_the_plain_env_value(self, tmp_path):
        """两者都给时文件优先：显式指向文件的意图更强，
        env 那一份常常只是历史遗留。"""
        path = tmp_path / "jwt"
        path.write_text("from-the-file", encoding="utf-8")
        settings = make_settings(
            tmp_path, jwt_secret="from-env", jwt_secret_file=str(path)
        )
        assert settings.jwt_secret == "from-the-file"

    def test_a_missing_file_refuses_to_start(self, tmp_path):
        with pytest.raises(ValidationError, match="JWT_SECRET_FILE"):
            make_settings(tmp_path, jwt_secret_file=str(tmp_path / "nope"))

    def test_an_empty_file_refuses_to_start(self, tmp_path):
        path = tmp_path / "jwt"
        path.write_text("   \n", encoding="utf-8")
        with pytest.raises(ValidationError, match="JWT_SECRET_FILE"):
            make_settings(tmp_path, jwt_secret_file=str(path))

    def test_the_same_convention_works_for_the_mail_password(self, tmp_path):
        path = tmp_path / "smtp"
        path.write_text("mail-secret", encoding="utf-8")
        settings = make_settings(tmp_path, smtp_password_file=str(path))
        assert settings.smtp_password == "mail-secret"

    def test_no_file_means_unchanged_behaviour(self, tmp_path):
        """不给 *_FILE 时行为必须和以前一模一样 —— 这是纯加成改动，
        老部署不该因为升级就开始找文件。"""
        settings = make_settings(tmp_path, jwt_secret="plain-env-secret")
        assert settings.jwt_secret == "plain-env-secret"
        assert settings.smtp_password == ""
