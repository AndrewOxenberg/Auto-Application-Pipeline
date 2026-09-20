/* middleware.ts - password gate for the hosted job pipeline (feature 20).
 *
 * Vercel runs this before every request, static files included, so without the
 * password neither the UI nor any /api route can be reached.
 * HTTP Basic auth: any username; the password must equal the SITE_PASSWORD env var.
 * Returning nothing lets the request through to the app.
 */

// Vercel's Edge runtime provides process.env; this project has no @types/node to say so.
declare const process: { env: Record<string, string | undefined> };

const REALM = 'Job pipeline';

function sameBytes(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

/* The password half of "Basic base64(user:password)", or null if absent/malformed. */
function suppliedPassword(header: string | null): string | null {
  const m = /^Basic\s+(\S+)$/i.exec(header || '');
  if (!m) return null;
  try {
    const bin = atob(m[1]);
    const text = new TextDecoder().decode(Uint8Array.from(bin, c => c.charCodeAt(0)));
    const colon = text.indexOf(':');
    return colon < 0 ? null : text.slice(colon + 1);
  } catch {
    return null;
  }
}

export default function middleware(request: Request): Response | undefined {
  const expected = process.env.SITE_PASSWORD;
  if (!expected) {
    // Fail closed: a deploy made before the password is set exposes nothing.
    return new Response('Site password not configured.\n', {
      status: 503,
      headers: { 'Cache-Control': 'no-store' }
    });
  }
  const given = suppliedPassword(request.headers.get('authorization'));
  const enc = new TextEncoder();
  if (given !== null && sameBytes(enc.encode(given), enc.encode(expected))) return undefined;
  return new Response('Password required.\n', {
    status: 401,
    headers: {
      'WWW-Authenticate': 'Basic realm="' + REALM + '", charset="UTF-8"',
      'Cache-Control': 'no-store'
    }
  });
}
