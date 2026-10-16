export class ApiError extends Error {
  readonly requestId: string | undefined
  readonly status: number

  constructor(message: string, requestId?: string, status = 0) {
    super(message)
    this.name = 'ApiError'
    this.requestId = requestId
    this.status = status
  }
}

let csrfToken: string | null = null

export function setCsrfToken(value: string | null) { csrfToken = value }

export async function api<T>(path: string, options: { method?: string; body?: unknown } = {}): Promise<T> {
  let response: Response
  const method = options.method ?? 'GET'
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (method !== 'GET') {
    headers['X-AION-Request'] = '1'
    if (csrfToken) headers['X-CSRF-Token'] = csrfToken
  }
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  try {
    response = await fetch(path, {
      credentials: 'same-origin',
      method, headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: AbortSignal.timeout(10000),
    })
  } catch {
    throw new ApiError('Не удалось связаться с сервисом. Попробуйте ещё раз.')
  }
  const requestId = response.headers.get('X-Request-ID') ?? undefined
  if (!response.ok) {
    let message = 'Сервис временно недоступен. Попробуйте чуть позже.'
    try {
      const payload = await response.json() as { error?: { message?: string } }
      if (payload.error?.message) message = payload.error.message
    } catch { /* Keep the readable fallback for non-JSON errors. */ }
    throw new ApiError(message, requestId, response.status)
  }
  if (response.status === 204) return undefined as T
  if (!response.headers.get('Content-Type')?.includes('application/json')) {
    throw new ApiError('Сервис вернул неожиданный ответ.', requestId)
  }
  try {
    return await response.json() as T
  } catch {
    throw new ApiError('Не удалось прочитать ответ сервиса.', requestId)
  }
}

export function getJson<T>(path: string): Promise<T> { return api<T>(path) }

export async function uploadFile(path: string, file: File): Promise<void> {
  const headers: Record<string,string> = { 'X-AION-Request':'1', 'Content-Type':'application/octet-stream' }
  if (csrfToken) headers['X-CSRF-Token']=csrfToken
  let response: Response
  try { response=await fetch(path,{method:'PUT',credentials:'same-origin',headers,body:file,signal:AbortSignal.timeout(60000)}) }
  catch { throw new ApiError('Загрузка прервалась. Повторите добавление файла.') }
  if (!response.ok) {
    const payload=await response.json().catch(()=>null) as {error?:{message?:string}} | null
    throw new ApiError(payload?.error?.message ?? 'Не удалось загрузить файл.',undefined,response.status)
  }
}
