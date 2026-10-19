export const API = process.env.PAPER_API_URL ?? 'http://127.0.0.1:8000';
export async function load(path: string) {
  try {
    const response = await fetch(`${API}${path}`, { cache: 'no-store', signal: AbortSignal.timeout(10000) });
    return response.ok ? response.json() : null;
  } catch {
    return null;
  }
}
