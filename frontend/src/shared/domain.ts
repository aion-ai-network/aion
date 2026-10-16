export interface Project { id: string; title: string }
export interface Task {
  id: string; title: string; description: string; first_step: string; project_id: string | null
  due_at: string | null; priority: number; estimated_minutes: number
  status: 'open' | 'done' | 'cancelled'; version: number; reason?: string
}
export interface Activity {
  id: string; title: string; notes: string; task_id: string | null; starts_at: string; ends_at: string
  fixed: boolean; keep_on_pause: boolean; notification_overrides: Record<string, boolean | null>
  outcome: 'unknown' | 'done' | 'partial' | 'cancelled'; result_text: string; version: number
}
export interface Plan {
  projects: Project[]; tasks: Task[]; activities: Activity[]
  focus: { mode: string; timezone: string; priorities: Task[]; activities: Activity[]; explanation: string }
}
export interface Capabilities {
  llm: boolean; voice: boolean; telegram: boolean; push: boolean; vapid_public_key: string; storage: string
}
export function localTime(value: string | null, zone: string): string {
  if (!value) return ''
  const parts = new Intl.DateTimeFormat('sv-SE', { timeZone: zone, year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date(value))
  const get = (type: string) => parts.find(p => p.type === type)?.value ?? ''
  return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`
}
export function displayTime(value: string, zone: string): string {
  return new Intl.DateTimeFormat('ru', { timeZone: zone, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }).format(new Date(value))
}
export function message(error: unknown): string { return error instanceof Error ? error.message : 'Не удалось выполнить действие.' }
