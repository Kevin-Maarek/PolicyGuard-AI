export async function api<T = any>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const response = await fetch("/api" + path, options);
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail || data),
    );
  return data;
}
export const post = <T = any>(path: string, body?: unknown) =>
  api<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
export const money = (amount: number) =>
  new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(amount);
export const time = (value?: string) =>
  value
    ? new Date(value).toLocaleString("en-US", {
        timeZone: "Asia/Jerusalem",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "Not yet synchronized";
