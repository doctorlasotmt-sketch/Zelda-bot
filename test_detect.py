"""Pruebas offline del detector de stock: python test_detect.py"""
import json
from bot import detect, IN_STOCK, OUT_OF_STOCK, UNKNOWN, BLOCKED

cfg = json.load(open("config.json"))
D = cfg["defaults"]
pad = "<p>" + "x " * 2000 + "</p>"

def ld(avail, price="519.99"):
    return ('<script type="application/ld+json">' + json.dumps(
        {"@type": "Product", "offers": {"@type": "Offer", "availability": avail, "price": price}}
    ) + "</script>")

cases = [
    ("JSON-LD InStock", ld("https://schema.org/InStock") + pad, IN_STOCK),
    ("JSON-LD OutOfStock", ld("http://schema.org/OutOfStock") + pad, OUT_OF_STOCK),
    ("JSON-LD InStock + 'sold out' en recomendados", ld("InStock") + pad + "<div>Sold Out</div>", IN_STOCK),
    ("Best Buy Sold Out", pad + '<button>Sold Out</button>', OUT_OF_STOCK),
    ("Target Add to cart", pad + '<button>Add to cart</button>', IN_STOCK),
    ("Agotado + add to cart de otro producto", pad + "<b>Producto agotado</b><button>Add to cart</button>", OUT_OF_STOCK),
    ("Tienda CL agregar al carro", pad + "<button>Agregar al carro</button>", IN_STOCK),
    ("Captcha", "<html>Please complete the captcha</html>", BLOCKED),
    ("Sin señales", pad, UNKNOWN),
]
ok = 0
for name, html, want in cases:
    got = detect(html, {}, D).status
    print(f"{'OK ' if got == want else 'FAIL'} {name}: {got}")
    ok += got == want
print(f"\n{ok}/{len(cases)} pasaron")
assert ok == len(cases)
