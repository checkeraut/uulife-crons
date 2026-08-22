// Scraper de uu.life — extrae info REAL y estructurada de la tienda (JSON-LD).
// Sin dependencias. El generador usa esto para fundamentar cada post en datos
// verdaderos (qué es, precio, categoría, stock), no en suposiciones.

const DEFAULT_BASE = 'https://uu.life/eu';

async function getHtml(url) {
  const res = await fetch(url, { headers: { 'user-agent': 'uulife-bot/1.0' } });
  if (!res.ok) throw new Error(`GET ${url} -> ${res.status}`);
  return res.text();
}

// Extrae todos los bloques JSON-LD (<script type="application/ld+json">) de una página.
function extractJsonLd(html) {
  const blocks = [];
  const re = /<script[^>]*type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(html))) {
    try {
      const parsed = JSON.parse(m[1].trim());
      if (Array.isArray(parsed)) blocks.push(...parsed);
      else blocks.push(parsed);
    } catch { /* bloque no parseable, lo saltamos */ }
  }
  return blocks;
}

// Lista de productos que se venden HOY (slugs), leída de /store.
export async function fetchProductSlugs(baseUrl = DEFAULT_BASE) {
  const html = await getHtml(`${baseUrl}/store`);
  const slugs = new Set();
  const re = /\/products\/([a-z0-9-]+)/gi;
  let m;
  while ((m = re.exec(html))) slugs.add(m[1]);
  return [...slugs];
}

// Datos estructurados de un producto puntual.
export async function fetchProduct(slug, baseUrl = DEFAULT_BASE) {
  const html = await getHtml(`${baseUrl}/products/${slug}`);
  const ld = extractJsonLd(html);
  const product = ld.find((o) => o['@type'] === 'Product') || {};
  const breadcrumb = ld.find((o) => o['@type'] === 'BreadcrumbList');
  let category = null;
  if (breadcrumb && Array.isArray(breadcrumb.itemListElement)) {
    const items = breadcrumb.itemListElement;
    // La categoría suele ser el penúltimo item del breadcrumb (antes del producto).
    const catItem = items.find((i) => i.position === items.length - 1);
    category = catItem?.name || null;
  }
  const offer = product.offers || {};
  return {
    slug,
    name: product.name || slug,
    description: (product.description || '').trim(),
    price: offer.price || null,
    currency: offer.priceCurrency || null,
    inStock: typeof offer.availability === 'string' ? /InStock/i.test(offer.availability) : null,
    category,
  };
}

// CLI rápido para inspeccionar: `node scraper.mjs [slug]`
if (import.meta.url === `file://${process.argv[1]}`) {
  const slug = process.argv[2];
  if (slug) {
    fetchProduct(slug).then((p) => console.log(JSON.stringify(p, null, 2))).catch((e) => { console.error(e.message); process.exit(1); });
  } else {
    fetchProductSlugs().then((s) => console.log(`${s.length} productos:\n` + s.join('\n'))).catch((e) => { console.error(e.message); process.exit(1); });
  }
}
