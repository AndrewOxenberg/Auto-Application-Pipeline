// Talks to the local pipeline server on behalf of the content script.
//
// The fetch lives here, not in content.js, because a content script's fetch is
// still subject to the page's CORS rules. A service worker with host_permissions
// is not, so no permissive CORS headers have to be added to the local server —
// which matters, since the packet carries personal data and the server should
// not hand it to arbitrary web pages.

const SERVER = "http://127.0.0.1:8765";

async function call(path, options) {
  const res = await fetch(SERVER + path, options);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
  if (msg?.type === "getPending") {
    call("/api/apply/pending")
      .then((data) => respond({ ok: true, data }))
      .catch((err) => respond({ ok: false, error: String(err.message || err) }));
    return true; // keep the channel open for the async reply
  }

  if (msg?.type === "getResume") {
    // Fetched here, not in the content script, and handed over as a plain
    // array so it survives the message channel. The content script turns it
    // back into a File and assigns it through DataTransfer.
    // Base64, not an array of 83,000 numbers. The array form serialises to
    // roughly half a megabyte of JSON through the message channel and was
    // slow enough that the fill ran before the bytes ever arrived.
    fetch(SERVER + "/api/resume")
      .then((r) => (r.ok ? r.arrayBuffer() : Promise.reject(new Error(String(r.status)))))
      .then((buf) => {
        const bytes = new Uint8Array(buf);
        let binary = "";
        const CHUNK = 0x8000; // btoa chokes on a spread of the whole array
        for (let i = 0; i < bytes.length; i += CHUNK) {
          binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
        }
        respond({ ok: true, b64: btoa(binary),
                  filename: "resume.pdf" });
      })
      .catch((err) => respond({ ok: false, error: String(err.message || err) }));
    return true;
  }

  if (msg?.type === "clearPending") {
    call("/api/apply/done", { method: "POST" })
      .then(() => respond({ ok: true }))
      .catch(() => respond({ ok: false }));
    return true;
  }

  return false;
});
