// Live star count on every [data-stars] element. GitHub's public API, cached for the visit; if it
// can't be reached the buttons simply show the star alone. The count appears from SHOW_FROM stars
// up, so a brand-new repo shows the star alone.
const SHOW_FROM = 10;

export async function mountStars() {
  const show = (n: number) => n >= SHOW_FROM && document.querySelectorAll<HTMLElement>("[data-stars]").forEach((el) => {
    el.textContent = n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n);
    el.hidden = false;
  });
  let cached: string | null = null;
  try { cached = sessionStorage.getItem("fs-stars"); } catch {}
  if (cached !== null) return void show(Number(cached));
  try {
    const response = await fetch("https://api.github.com/repos/yail259/frankensurf", { headers: { Accept: "application/vnd.github+json" } });
    if (!response.ok) return;
    const n = (await response.json()).stargazers_count;
    if (typeof n !== "number") return;
    try { sessionStorage.setItem("fs-stars", String(n)); } catch {}
    show(n);
  } catch {}
}
