import { apiGet } from "../lib/api";
import type { UserOut } from "../lib/types";
import { useApp } from "../store/app";
import { useAsync } from "../components/useAsync";
import { Empty, ErrBox, Note, Skel, Tag } from "../components/bits";

/* 用户目录。管理员端点。
 *
 * 响应里**没有** email 和 password_hash —— schemas.UserOut 只有
 * id / username / role / certs 四个字段。这不是漏了，是故意的：
 * 界面上不该出现的东西，最省事的做法是后端根本不下发。
 * 这条也是这个面板存在的教学意义：一个"管理页"最容易顺手多查几列。
 */

export function UsersPane() {
  const { isAdmin, setUsers } = useApp();

  const state = useAsync<{ rows: UserOut[]; total: number }>(
    async (signal) => {
      if (!isAdmin) return { rows: [], total: 0 };
      const { data, headers } = await apiGet<UserOut[]>("/api/users?limit=200", signal);
      // 总数从响应头来，不从数组长度来：limit 截断过之后，
      // 数组长度和"一共有多少个账号"是两件事。混用会让文案在超限时撒谎。
      const total = Number(headers.get("X-Total-Count") || data.length);
      setUsers(data, total);
      return { rows: data, total };
    },
    [isAdmin, setUsers],
  );

  if (!isAdmin) {
    return <Empty>当前身份无权查看用户目录（该端点需要管理员）</Empty>;
  }
  if (state.loading && !state.data) return <Skel rows={3} />;
  if (state.error) return <ErrBox error={state.error} onRetry={state.reload} what="加载用户目录" />;

  const rows = state.data?.rows ?? [];
  const total = state.data?.total ?? 0;

  return (
    <div>
      <Note>
        用户目录为管理员端点（GET /api/users）。响应中不含邮箱与口令哈希 —— 只回显业务需要的字段。
      </Note>

      {total > rows.length ? (
        <div className="hint mt-2">共 {total} 个账号，此处展示前 {rows.length} 个。</div>
      ) : null}

      <table className="dt mt-2">
        <thead>
          <tr><th>ID</th><th>用户</th><th>角色</th><th>准入资质</th></tr>
        </thead>
        <tbody>
          {rows.map((u) => (
            <tr key={u.id}>
              <td className="mono text-ink-3">{u.id}</td>
              <td>{u.username}</td>
              <td>
                {u.role === "user" ? <Tag>普通用户</Tag> : <Tag tone="warn">{u.role === "sysadmin" ? "系统管理员" : "管理员"}</Tag>}
              </td>
              <td className="text-ink-2">{u.certs.length ? u.certs.join(" / ") : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
