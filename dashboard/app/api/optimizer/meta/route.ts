import { NextResponse } from 'next/server';
import { pool } from '@/lib/db';

export async function GET() {
  const { rows } = await pool.query(
    `SELECT iso_week, summary, report, generated_at
       FROM optimizer_meta_reports
      ORDER BY generated_at DESC
      LIMIT 12`,
  );
  return NextResponse.json({ reports: rows });
}
