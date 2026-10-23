import { apiGet } from "../lib/api";
import type { LabOut } from "../lib/types";
import { useApp } from "../store/app";
import { useAsync } from "../components/useAsync";
import { ErrBox, Skel, Tag } from "../components/bits";

/* 实验室与设备。
 *
 * 一个历史坑记在这里：后端字段叫 **label**，不是 name。
 * 写成 l.name 会渲染成 undefined，而且不报错 —— 页面看起来"少了个标题"，
 * 不是"出错了"。所以这个文件里没有任何一处 .name，除了设备自己的 name。
 */

const fmtHours = (v: [string, string] | null) => (v ? v[0] + "-" + v[1] : "—");

export function LabsPane() {
  const { setLabs } = useApp();
  const state = useAsync<LabOut[]>(async (signal) => {
    const { data } = await apiGet<LabOut[]>("/api/labs", signal);
    // 这一行是**必须**的：门禁面板的实验室下拉、下单表单的设备下拉
    // 都从 context 里取。原来只存了局部变量，于是"列表渲染出来了，
    // 但下拉里一条都没有" —— 渲染出来了 ≠ 能用。
    setLabs(data);
    return data;
  }, [setLabs]);

  if (state.loading && !state.data) return <Skel rows={2} card />;
  if (state.error) return <ErrBox error={state.error} onRetry={state.reload} what="加载设备目录" />;
  const labs = state.data ?? [];

  return (
    <div className="flex flex-col gap-4">
      {labs.map((lab) => (
        <div key={lab.id} className="rounded-md border border-line">
          <div className="flex flex-wrap items-center gap-2 border-b border-line-soft px-3 py-2">
            <b className="text-md">{lab.label}</b>
            <Tag>容纳 {lab.capacity} 人</Tag>
            <Tag>工作日 {fmtHours(lab.open_hours.weekday)}</Tag>
            <Tag>周末 {fmtHours(lab.open_hours.weekend)}</Tag>
          </div>
          {lab.note ? <div className="px-3 py-1.5 text-sm text-ink-3">{lab.note}</div> : null}
          <table className="dt">
            <thead>
              <tr>
                <th>编号</th><th>名称</th><th>类别</th><th>单次上限</th><th>准入</th><th>状态</th>
              </tr>
            </thead>
            <tbody>
              {lab.equipment.map((e) => (
                <tr key={e.id}>
                  <td className="mono text-sm text-ink-3">{e.code}</td>
                  <td>{e.name}</td>
                  <td className="text-ink-2">{e.category}</td>
                  <td className="mono">{e.max_hours}h</td>
                  <td>{e.requires_training ? <Tag tone="warn">需资质</Tag> : <Tag>免资质</Tag>}</td>
                  <td>{e.status === "normal" ? <Tag tone="ok">可用</Tag> : <Tag tone="bad">{e.status}</Tag>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}
