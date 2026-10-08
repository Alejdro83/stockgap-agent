APPROVED
G4-N1 está resuelto. El literal `0xb471f1ab267144cf4cfff4800bea3ebc0e08ae64` es igual a `'0xb471f1aB267144CF4CFff4800beA3ebC0E08ae64'.lower()` (comprobado con python3). Con grep aparece igual en prueba1-runplan.md:66 (paso 3), :71 (02c del paso 8) y :95. En los dos sitios se compara `payTo.lower()`. La forma con mayúsculas mezcladas solo queda en :15 y :63 (config por env), que es correcto porque el challenge devuelve `cfg.merchant` sin cambios (b402_gate.py:254,80) y `verify_receipt` normaliza con `.lower()` (b402_gate.py:135,144).
G4-N2 está resuelto. La rama depende del estado de la copia, comprobado antes del paso 5 con `SELECT COUNT(*) FROM sqlite_master WHERE name='paid_calls'` (prueba1-runplan.md:68, :80, :94). Si da 0, después del paso 5 se exige `COUNT(*) FROM paid_calls` = 0. Es coherente con el código: H_fail es un proof no vacío, así que `PaidCalls(self.db_path)` crea la tabla en b402_gate.py:268 antes de `verify_receipt`. El rechazo llega en b402_gate.py:152-153 sin pasar por `claim` (:279), y la tabla queda vacía. Si da 1, se mide el COUNT antes y después, con delta 0.
Sin regresiones: G1-N1…N12, G2-N1…N6 y G3-N1…N2 siguen presentes y correctos en rev 5. Las citas cuadran con el código: b402_gate.py:28/34-35/77-87/130-162/171-174/253-256/263-268/279, mcp_server.py:36-37/40-46 (lectura ro de la copia)/68-79/209-214, config/mcp.env:11/19/20, dashboard.py:327, run-plain.sh:30, sellerCore.ts:49-50/446 y package.json:17.
No hay defectos nuevos en el texto de rev 5.
Entorno reverificado el 2026-10-07:
- :8098 = pid 1695012 y :8198 libre.
- eth_chainId = 0x61 en data-seed-prebsc-1-s1 y en publicnode.
- 0xb471…ae64 y 0x069a…f819 = 0x429d069189e0000 cada una.
- Nonce de 0xb471…ae64 = 0x0.
- data/prueba1-* no existe.
