import { NextResponse } from 'next/server';
import { pool } from '@/lib/db';

export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as {
    action?: string;
    source?: string;
    enabled?: boolean;
    auto_apply?: boolean;
  };
  const action = body.action;
  switch (action) {
    case 'disable_optimizer': {
      await pool.query(
        `INSERT INTO config (key,value,updated_by)
           VALUES ('OPTIMIZER_ENABLED','false'::jsonb,'dashboard')
         ON CONFLICT (key) DO UPDATE
           SET value='false'::jsonb, updated_by='dashboard', updated_at=NOW()`,
      );
      return NextResponse.json({ ok: true });
    }
    case 'enable_optimizer': {
      await pool.query(
        `INSERT INTO config (key,value,updated_by)
           VALUES ('OPTIMIZER_ENABLED','true'::jsonb,'dashboard')
         ON CONFLICT (key) DO UPDATE
           SET value='true'::jsonb, updated_by='dashboard', updated_at=NOW()`,
      );
      return NextResponse.json({ ok: true });
    }
    case 'force_rollback': {
      
      await pool.query(
        `INSERT INTO audit_log (actor, action, details)
           VALUES ('dashboard','force_rollback_requested','{}'::jsonb)`,
      );
      await pool.query(
        `INSERT INTO config (key,value,updated_by)
           VALUES ('_force_rollback_pending','true'::jsonb,'dashboard')
         ON CONFLICT (key) DO UPDATE
           SET value='true'::jsonb, updated_by='dashboard', updated_at=NOW()`,
      );
      return NextResponse.json({ ok: true });
    }
    case 'set_source_flag': {
      if (typeof body.source !== 'string') {
        return NextResponse.json({ error: 'source required' }, { status: 400 });
      }
      const fields: string[] = [];
      const params: (string | boolean)[] = [body.source];
      if (typeof body.enabled === 'boolean') {
        params.push(body.enabled);
        fields.push(`enabled=$${params.length}`);
      }
      if (typeof body.auto_apply === 'boolean') {
        params.push(body.auto_apply);
        fields.push(`auto_apply=$${params.length}`);
      }
      if (fields.length === 0) {
        return NextResponse.json({ error: 'nothing to update' }, { status: 400 });
      }
      await pool.query(
        `UPDATE optimizer_source_flags
            SET ${fields.join(', ')}, updated_at=NOW(), updated_by='dashboard'
          WHERE source=$1`,
        params,
      );
      return NextResponse.json({ ok: true });
    }
    default:
      return NextResponse.json({ error: `unknown action ${action}` }, { status: 400 });
  }
}
