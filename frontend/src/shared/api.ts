export class ApiError extends Error {
  readonly requestId: string | undefined

  constructor(message: string, requestId?: string) {
    super(message)
    this.name = 'ApiError'
    this.requestId = requestId
  }
}

export async function getJson<T>(path: string): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(6000),
    })
  } catch {
    throw new ApiError('Не удалось связаться с сервисом. Попробуйте ещё раз.')
  }
  const requestId = response.headers.get('X-Request-ID') ?? undefined
  if (!response.ok) {
    throw new ApiError('Сервис временно недоступен. Попробуйте чуть позже.', requestId)
  }
  if (!response.headers.get('Content-Type')?.includes('application/json')) {
    throw new ApiError('Сервис вернул неожиданный ответ.', requestId)
  }
  try {
    return await response.json() as T
  } catch {
    throw new ApiError('Не удалось прочитать ответ сервиса.', requestId)
  }
}
