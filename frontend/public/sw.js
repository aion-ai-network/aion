self.addEventListener('push', event => {
  let data = {}
  try { data = event.data?.json() ?? {} } catch { return }
  event.waitUntil(self.registration.showNotification(String(data.title || 'AION'), {
    body: String(data.body || ''), tag: String(data.tag || 'aion'),
    data: { url: '/notifications' }, renotify: false,
  }))
})
self.addEventListener('notificationclick', event => {
  event.notification.close()
  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async windows => {
    const target = new URL('/notifications', self.location.origin).href
    const current = windows.find(w => new URL(w.url).origin === self.location.origin)
    if (current) { await current.navigate(target); return current.focus() }
    return clients.openWindow(target)
  }))
})
