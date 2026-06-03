'use client';
import { useState, useTransition } from 'react';
import type { StatusInfo } from '@/lib/state';

export function ModeToggles({ status }: { status: StatusInfo }) {
  const [pending, startTransition] = useTransition();
  const [err, setErr] = useState<string | null>(null);

  const toggle = (key: string, value: boolean) => {
    setErr(null);
    startTransition(async () => {
      try {
        const r = await fetch('/Trader/api/mode', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ key, value }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        window.location.reload();
      } catch (e) {
        setErr(e instanceof Error ? e.message : 'failed');
      }
    });
  };

  return (
    <div className="mode-row">
      <Toggle
        label="Manual approval"
        hint="Queue buys for human click-through instead of auto-executing."
        checked={status.manualApprovalMode}
        disabled={pending}
        onChange={(v) => toggle('MANUAL_APPROVAL_MODE', v)}
      />
      <Toggle
        label="News watcher"
        hint="Every 15 min, Claude re-checks news on held positions and can force-exit."
        checked={status.newsWatcherEnabled}
        disabled={pending}
        onChange={(v) => toggle('NEWS_WATCHER_ENABLED', v)}
      />
      {err && <div style={{ color: 'var(--red)', fontSize: 12 }}>{err}</div>}
    </div>
  );
}

function Toggle({
  label, hint, checked, disabled, onChange,
}: {
  label: string; hint: string; checked: boolean; disabled: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className={`toggle-card ${checked ? 'on' : ''}`}>
      <input
        type="checkbox" checked={checked} disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <div>
        <div className="toggle-label">{label}{checked ? ' · on' : ' · off'}</div>
        <div className="toggle-hint">{hint}</div>
      </div>
    </label>
  );
}
