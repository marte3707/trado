self.addEventListener("push", event => {
  const data = event.data?.json() ?? {};
  event.waitUntil(
    self.registration.showNotification(data.title ?? "Trado", {
      body: data.body ?? "",
      icon: "/favicon.ico",
      tag: data.tag ?? "trado",
      renotify: true,
      data: { url: data.url ?? "/" },
    })
  );
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
      const win = list.find(c => "focus" in c);
      if (win) return win.focus();
      return clients.openWindow(event.notification.data?.url ?? "/");
    })
  );
});
