// ============================================================================
// 跟进预警 —— 系统兜底，不依赖"有人记得来拉"
//
// 分级不是为了好看：超过 2 倍 SLA 的线索基本就是白投的广告费，必须先处理。
// 认领 = 我接手这件事；不认领就一直挂着升级，不会被"已读"糊弄过去。
// ============================================================================
import { useMemo, useState } from "react";
import { api, ApiError, type SopAlert } from "../api";
import { actionLabel, fmtAgo, ownerLabel, stageLabel, useAsync } from "../hooks";
import { Avatar, Badge, Button, Card, Empty, Metric, Notice, SkeletonRows, useToast } from "../components/ui";
import { LeadDrawer } from "../components/LeadDrawer";

export function AlertsPage({ owner, onChanged }: { owner: string; onChanged: () => void }) {
  const { notify } = useToast();
  const [includeAck, setIncludeAck] = useState(false);
  const alerts = useAsync(() => api.flow.alerts(includeAck), [includeAck]);
  const [openLead, setOpenLead] = useState<string | null>(null);
  const [busy, setBusy] = useState("");

  const items = useMemo(() => {
    const list = alerts.data?.alerts ?? [];
    const filtered = owner ? list.filter((a) => a.owner === owner) : list;
    return filtered.slice().sort((a, b) => b.overdue_minutes - a.overdue_minutes);
  }, [alerts.data, owner]);

  const critical = items.filter((a) => a.overdue_minutes >= 120);
  const normal = items.filter((a) => a.overdue_minutes < 120);

  const sweep = async () => {
    setBusy("sweep");
    try {
      await api.flow.sweep();
      notify("已扫描，新产生的预警会立刻显示");
      alerts.reload();
      onChanged();
    } catch (exc) {
      notify(exc instanceof ApiError ? exc.message : "扫描失败", "error");
    } finally {
      setBusy("");
    }
  };

  const ack = async (a: SopAlert) => {
    setBusy(a.alert_id);
    try {
      await api.flow.ackAlert(a.alert_id, owner || "mgr_01");
      notify("已认领，请在一个 SLA 内处理");
      alerts.reload();
      onChanged();
    } catch (exc) {
      notify(exc instanceof ApiError ? exc.message : "认领失败", "error");
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="stack gap-16">
      <div className="g-kpi" style={{ gap: 12 }}>
        <Metric
          label="未处理预警"
          value={items.filter((a) => !a.acknowledged_by).length}
          unit="条"
          icon="alert"
          tone={items.filter((a) => !a.acknowledged_by).length > 0 ? "danger" : undefined}
          foot="系统判定超过 SOP 时限"
        />
        <Metric label="超过 2 小时" value={critical.length} unit="条" icon="clock" tone={critical.length > 0 ? "danger" : undefined} foot="基本等于这条线索要废" />
        <Metric label="已认领待处理" value={items.filter((a) => a.acknowledged_by).length} unit="条" icon="check" foot="认领后仍在时限内跟进" />
        <Metric label="覆盖业务员" value={new Set(items.map((a) => a.owner)).size} unit="人" icon="users" foot="预警按负责人归属" />
      </div>

      <div className="row gap-10 wrap">
        <div className="grow" />
        <Button
          size="sm"
          variant={includeAck ? "soft" : "ghost"}
          icon={includeAck ? "check" : "file"}
          onClick={() => setIncludeAck((v) => !v)}
        >
          {includeAck ? "正在显示已认领" : "显示已认领"}
        </Button>
        <Button size="sm" icon="refresh" onClick={sweep} loading={busy === "sweep"}>
          立即扫描
        </Button>
      </div>

      <Notice tone="info" icon="clock">
        <b>规则：</b>首呼 30 分钟、加微 2 小时、跟进 24 小时、邀约后确认 24 小时。
        超时先记预警，超过 <b>2 倍时限</b>才升级到主管 —— 避免临界抖动天天报警。
      </Notice>

      {alerts.error && <Notice tone="danger">{alerts.error}</Notice>}

      {alerts.loading && !alerts.data ? (
        <Card pad>
          <SkeletonRows rows={4} height={64} />
        </Card>
      ) : items.length === 0 ? (
        <Card>
          <Empty
            icon="shield"
            title="所有线索都在时限内"
            desc="系统每 5 分钟扫描一次。如果客户一直不接电话，记得在详情里写清楚，别让它自然过期。"
          />
        </Card>
      ) : (
        <div className="stack gap-16">
          {critical.length > 0 && (
            <Card>
              <div className="card-head">
                <div className="grow">
                  <h3 style={{ color: "var(--danger)" }}>需要立刻处理 · 超时 2 小时以上</h3>
                  <div className="hint">这批客户大概率已经在别家问过了</div>
                </div>
                <Badge tone="red">{critical.length} 条</Badge>
              </div>
              <div style={{ padding: "4px 10px 10px" }}>
                {critical.map((a) => (
                  <AlertRow
                    key={a.alert_id}
                    alert={a}
                    busy={busy === a.alert_id}
                    onAck={() => void ack(a)}
                    onOpen={() => setOpenLead(a.lead_id)}
                  />
                ))}
              </div>
            </Card>
          )}

          {normal.length > 0 && (
            <Card>
              <div className="card-head">
                <div className="grow">
                  <h3>刚超时 · 还能救</h3>
                  <div className="hint">优先打电话，微信可能已经被埋下去了</div>
                </div>
                <Badge tone="amber">{normal.length} 条</Badge>
              </div>
              <div style={{ padding: "4px 10px 10px" }}>
                {normal.map((a) => (
                  <AlertRow
                    key={a.alert_id}
                    alert={a}
                    busy={busy === a.alert_id}
                    onAck={() => void ack(a)}
                    onOpen={() => setOpenLead(a.lead_id)}
                  />
                ))}
              </div>
            </Card>
          )}
        </div>
      )}

      <LeadDrawer
        leadId={openLead}
        onClose={() => setOpenLead(null)}
        onChanged={() => {
          alerts.reload();
          onChanged();
        }}
      />
    </div>
  );
}

function AlertRow({
  alert,
  busy,
  onAck,
  onOpen,
}: {
  alert: SopAlert;
  busy: boolean;
  onAck: () => void;
  onOpen: () => void;
}) {
  return (
    <div className="row gap-12" style={{ padding: "12px 10px", borderBottom: "1px solid var(--line)" }}>
      <Avatar name={alert.customer_name} size="sm" />
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="row gap-8 wrap">
          <span className="t-sm w-500">{alert.customer_name}</span>
          <Badge tone={alert.overdue_minutes >= 120 ? "red" : "amber"}>超时 {alert.overdue_minutes} 分钟</Badge>
          {alert.acknowledged_by && <Badge tone="blue">已认领 · {alert.acknowledged_by}</Badge>}
        </div>
        <div className="t-xs dim" style={{ marginTop: 3 }}>
          {stageLabel(alert.stage)} · 该做：{actionLabel(alert.action)} · 负责人 {ownerLabel(alert.owner)} · 产生于 {fmtAgo(alert.created_at)}
        </div>
      </div>
      <div className="row gap-8" style={{ flex: "none" }}>
        {!alert.acknowledged_by && (
          <Button size="sm" icon="check" onClick={onAck} loading={busy}>
            认领
          </Button>
        )}
        <Button size="sm" variant="soft" onClick={onOpen}>
          去处理
        </Button>
      </div>
    </div>
  );
}
