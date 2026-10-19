/** Next 16 route params retain URI encoding; decode once before an API request. */
export function decodePaperId(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}
