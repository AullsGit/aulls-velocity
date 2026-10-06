// Cliente da API. Erros chegam sempre como {error: {code, message}} — nunca HTML/tracebacks.
export class ApiErr extends Error {
  constructor(status, code, message, extra = {}) { super(message); this.status = status; this.code = code; this.extra = extra; }
}

export async function api(path, { method = "GET", body, headers = {}, signal } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method, signal, credentials: "same-origin",
      headers: { ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...headers },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiErr(0, "network", "No connection. Check your internet and try again.");
  }
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) {
    const err = (data && data.error) || {};
    if (res.status === 401 && !path.startsWith("/api/auth")) window.dispatchEvent(new Event("awls:unauthorized"));
    const { code, message, ...extra } = err;
    throw new ApiErr(res.status, code || "error", message || "Something went wrong. Please try again.", extra);
  }
  return data;
}

export const uid = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`);

// Upload direto para o Storage (URL assinada pelo backend): PUT multipart, campo "file".
export function uploadTo(upload, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(upload.method || "PUT", upload.url);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress && onProgress(e.loaded / e.total); };
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new ApiErr(xhr.status, "upload_failed", "Upload failed. Please try again.")));
    xhr.onerror = () => reject(new ApiErr(0, "network", "Upload interrupted. Check your connection and try again."));
    const fd = new FormData();
    fd.append(upload.field || "file", file, file.name);
    xhr.send(fd);
  });
}

// Lê duração, dimensões e uma miniatura no próprio browser (antes de enviar), para validar
// limites e mostrar pré-visualização imediata.
export function probeMedia(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    if (file.type.startsWith("image/")) {
      const img = new Image();
      img.onload = () => resolve({ url, duration: null, width: img.naturalWidth, height: img.naturalHeight, thumb: url });
      img.onerror = () => resolve({ url, duration: null, thumb: null });
      img.src = url;
      return;
    }
    if (file.type.startsWith("audio/")) {
      const a = document.createElement("audio");
      a.preload = "metadata";
      a.onloadedmetadata = () => resolve({ url, duration: a.duration, thumb: null });
      a.onerror = () => resolve({ url, duration: null, thumb: null });
      a.src = url;
      return;
    }
    const v = document.createElement("video");
    v.muted = true; v.playsInline = true; v.preload = "metadata";
    let done = false;
    const finish = (thumb) => { if (done) return; done = true; resolve({ url, duration: isFinite(v.duration) ? v.duration : null, width: v.videoWidth, height: v.videoHeight, thumb }); };
    v.onloadedmetadata = () => { v.currentTime = Math.min(0.5, (v.duration || 1) / 3); };
    v.onseeked = () => {
      try {
        const c = document.createElement("canvas");
        const w = 180, hh = Math.round(w * (v.videoHeight || 16) / (v.videoWidth || 9));
        c.width = w; c.height = hh;
        c.getContext("2d").drawImage(v, 0, 0, w, hh);
        finish(c.toDataURL("image/jpeg", 0.7));
      } catch { finish(null); }
    };
    v.onerror = () => finish(null);
    setTimeout(() => finish(null), 8000);
    v.src = url;
  });
}

// Polling com recuo progressivo e pausa com o separador escondido.
export function pollJob(jobId, onUpdate, { min = 1500, max = 6000 } = {}) {
  let stopped = false, delay = min, timer;
  const tick = async () => {
    if (stopped) return;
    if (document.hidden) { timer = setTimeout(tick, 1500); return; }
    try {
      const { job, credits } = await api(`/api/jobs/${jobId}`);
      onUpdate(job, credits);
      if (["COMPLETED", "FAILED", "CANCELLED"].includes(job.status)) { stopped = true; return; }
      delay = job.status === "QUEUED" ? Math.min(max, delay * 1.3) : min + 500;
    } catch (e) {
      delay = Math.min(max * 2, delay * 1.6);
    }
    timer = setTimeout(tick, delay);
  };
  tick();
  return () => { stopped = true; clearTimeout(timer); };
}
