import { computeState } from '@/lib/state';
import { Dashboard } from './Dashboard';

export const dynamic = 'force-dynamic';
export const revalidate = 0;

export default async function Home() {
  const initial = await computeState();
  return <Dashboard initial={initial} />;
}
