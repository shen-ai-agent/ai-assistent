// Service worker cho PWA. Chỉ bắt yêu cầu mở trang: mất kết nối tới server thì hiện offline.html.
// Không cache CSS/JS/API: server gửi no-cache để sửa web là thấy ngay, giữ nguyên như vậy.
const CACHE = "mimi-offline-v1";
const OFFLINE = ["offline.html", "icons/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(OFFLINE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate") return; // để trình duyệt tự xử lý như không có service worker
  e.respondWith(fetch(e.request).catch(() => caches.match("offline.html")));
});
