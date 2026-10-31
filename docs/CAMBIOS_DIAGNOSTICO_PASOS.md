# Diagnóstico de los pasos del portal

Fecha: 2026-10-31. Rama: `diagnostico-pasos-portal`.
Base: `origin/main` en `5023f54` (PR #4 fusionado). No se cambió `main` ni se desplegó el servicio.

## Resultado principal

Ahora se puede distinguir qué control eligió el bot, qué página quedó, cuántos campos había y por qué se detuvo. **No se identificó la causa del incidente real:** no se consultó su página ni se usó el ticket del usuario. La reproducción local descubre una barrera concreta de inicio de sesión, que se conserva; no se presume que sea la misma del portal real.

Se encontró y corrigió un defecto independiente, pequeño y general: un formulario podía anunciarse como listo aunque tuviera un control obligatorio no compatible. El caso de prueba falla antes de corregirlo y pasa después. No se añadió ninguna excepción por comercio, dominio, asistente, encuesta ni texto de botón.

## Qué se agregó

Los eventos siguen siendo JSON `portal_flow` del logger `personal_agent.portal`, con las claves anteriores (`tool`, `phase`, `code`, `scope_changed`, `browser_review_exists`, `permission_current`). Los nuevos eventos añaden:

| Campo | Qué significa |
|---|---|
| `step` | Posición del recorrido: apertura = 1; cada acción del navegador consume un paso. Observar otra vez no lo incrementa. |
| `action` | `click` (clic), `write` (llenado aprobado), `wait` (apertura, observación, filtro o parada). |
| `role`, `element_text` | Rol del control y texto visible o nombre accesible; máximo 80 caracteres, saneado antes de registrar. En paradas sin un elemento elegido se usa `unknown` / `sin_nombre`. |
| `host`, `path` | Página que quedó después del paso; nunca usuario/contraseña, query ni fragmento. Se omiten datos privados que aparezcan en el camino. |
| `fields_before`, `fields_after` | Cantidades de controles de formulario observados; nunca sus valores. |
| `duration_ms` | Duración de la operación del navegador o llenado confirmado. |
| `elapsed_ms` | Tiempo acumulado del recorrido al guardar su última observación; se muestra en la parada. No incluye la petición inicial del modelo ni el envío del mensaje de Telegram. Los eventos operativos que no tienen ese reloj usan 0. |
| `reason`, `filter` | Razón de parada o filtro, como constantes del servidor; sin texto libre del modelo o de la página. |

Fases nuevas: `navigation_step`, `navigation_error`, `action_filter`, `stop`. Un evento `navigation_error` documenta el intento fallido; no confirma que se ejecutó un clic. `action_filter` muestra por qué un candidato no entró al atajo automático; **no implica que haya terminado el trabajo del agente**. El modelo aún puede elegir una referencia pública observada. La fase `stop` de `telegram_portal` resume la parada que se presenta al usuario.

Razones fijas: `form_found`, `step_limit`, `time_limit`, `no_actionable_element`, `action_filtered`, `permission_required`, `captcha_or_unfillable`, `error`. Los presupuestos del modelo y las herramientas se identifican además con `filter=model_budget`; no se confunden con falta de botones. Un ciclo de acciones se identifica como `cycle`.

Telegram conserva la vista del portal y añade una explicación: tiempo agotado en el paso indicado, límite de pasos, falta de permiso, ausencia de controles, controles filtrados, error o ayuda humana por verificación/inicio de sesión. La explicación aparece también cuando hay algunos campos visibles; no depende de que la página esté vacía.

Para controles sin texto visible se obtiene el nombre en el orden solicitado: `aria-label`, `title`, `alt` de la imagen y nombre de su archivo, sin ruta, query ni extensión. Si no existe, `sin_nombre`. Estos metadatos son **separados** de la etiqueta usada por la selección existente; no se cambió la navegación para favorecer un nombre concreto. Las cinco variantes de nombre se prueban en la misma fixture de formulario inicial, sin crear otros tipos de portal.

Los llenados aprobados también registran `write`, con la etiqueta del campo y sus cantidades, después de confirmar el resultado. No se registran los valores copiados ni se concede una aprobación nueva.

## Privacidad

`portal_diagnostics.py` construye y valida un esquema cerrado. Rechaza claves adicionales, acciones, roles o razones no admitidos; limita el texto y los contadores. Antes de emitir, sanea RFC, correos, números, URLs/query y valores privados del ticket y perfil, incluyendo palabras del nombre fiscal. Tanto el constructor como la entrada final al logger validan el evento. Los caminos con datos privados pueden aparecer como `[omitido]`; se prioriza no revelar el dato sobre conservar esa parte del camino.

No se leen valores de inputs para nombrar controles; no se registran contenido de páginas, RFC, nombre, correo, importe, folio, referencia de compra, campos escritos, argumentos, claves ni tokens de URL. Los números de paso, cantidades y tiempos son contadores calculados por el servidor.

Las pruebas intentan emitir eventos contaminados con RFC, correo, nombre fiscal, folio e importe: **la validación falla y el logger no emite nada**. Otra prueba llena la fixture local con datos ficticios, comprueba que los eventos solo contienen `RFC` y `Ticket` como etiquetas y rechaza un intento de introducir el nombre fiscal directamente por `portal_diagnostic`.

Se actualizó la aserción de esquema del test anterior `test_first_observe_with_field_meanings_opens_before_interpreting`: sigue exigiendo exactamente las claves originales o exactamente esas claves más el esquema nuevo, y valida cada evento nuevo. No se relajaron comprobaciones de propietario, permisos o datos.

## Revisión de filtros (Objetivo 1.2)

| Lugar | Hallazgo | ¿Se modificó? |
|---|---|---|
| `backend/app/billing/receipt.py:72` | `unrelated_billing_link` rechaza direcciones o evidencia con encuesta, survey, feedback, opinión o satisfacción. Puede descartar una entrada impresa cuya dirección tenga esos términos, aunque un sitio use ese camino antes de facturar. | No. No se demostró que fuera la causa del caso real ni de la fixture. |
| `backend/app/billing/service.py:330`, `portal_discovery.py:20`, `portal_memory.py:102` | Ese filtro se aplica al elegir/comprobar entradas y memorias. No se aplica al texto de cada botón de la página una vez abierta. | No. |
| `backend/app/billing/portal_navigation.py:20` | El atajo automático prefiere indicios de facturación, menú o marca coincidente; no elige cualquier botón visible. Tiene un filtro de login/registro, avisos, emisión, envío, etc. | No se cambió la preferencia ni la lista de palabras. `action_filter` explica las decisiones existentes. |
| `backend/browser_worker/engine.py:89` | No expone como navegación pública botones de envío ni ciertos controles asociados a un formulario; descarta destinos vacíos, controles ya expandidos, enlaces de seguimiento, a la misma página o no admitidos. | Solo se añadieron metadatos de los descartes; las exclusiones se conservan. |
| `backend/browser_worker/policy.py:42` | `tracking_url` filtra hosts de seguimiento; no clasifica una página como encuesta por su texto. | No. |
| `backend/browser_worker/engine.py:115`, `service.py:533`, `engine.py:644` | Un password visible con indicios de login marca `login_required`; servicio y worker impiden actuar en esa pantalla. La opción pública sin cuenta no crea una excepción a esa barrera. | No. |

En la fixture, «Continuar sin evaluar» se registra como `not_billing_hint` para el atajo automático, pero **el agente de prueba sí lo selecciona y se ejecuta el clic**. No existe una prohibición global de ese texto. La parada posterior es `login_required`, no el filtro de encuestas. Relajar la barrera de login para admitir excepciones por enlaces anónimos requiere una decisión y un diseño general de seguridad; no se hizo como parche basado en una frase.

## Límites y medición (Objetivo 1.3)

- `billing/tools.py:235`: plazo de ruta `time.monotonic()+22`, creado antes de la primera apertura. Los siguientes pasos del mismo trabajo conservan ese plazo. `tools.py:180` y `:264` limitan el avance a siete acciones con permiso de ruta; sin ese permiso de recorrido el avance del modelo se limita a una acción. No se alteraron esos límites.
- `browser_worker/engine.py:161`: ocho pasos por sesión (apertura y hasta siete acciones), TTL de 180 s; volver a observar no consume otro paso. El contador del worker se devuelve explícitamente en la observación.
- `engine.py:474`: apertura de hasta 10 s, `goto` de hasta 8 s; observación hasta 6 s, espera de carga de hasta 1 s y espera de selector de hasta 1,2 s. Son límites superpuestos, no tiempos que todas las páginas consuman.
- Herramientas públicas: 30 s; HTTP al worker: 15 s por llamada; ejecución SDK: 35 s, tres turnos de modelo y cinco herramientas por defecto (`config.py:29`). Un paso iniciado antes del segundo 22 puede terminar después; ese plazo se comprueba antes de iniciar nuevas acciones. No se garantiza que todas las combinaciones de máximos quepan en él.

Un asistente con tres pantallas y dos transiciones consumiría **tres pasos del navegador** y **dos de las siete acciones**; si requiere que el modelo elija cada transición, puede utilizar los tres turnos (observar, navegar, navegar). **No se midió su tiempo completo porque la fixture se detiene antes de la tercera pantalla.** No se saltó el login para fabricar una medición.

Medición de la corrida focalizada de 25 pruebas, con Chromium real pero páginas y modelo simulados:

| Caso | Pasos y acciones ejecutados | `elapsed_ms` al último estado | Interpretación |
|---|---|---:|---|
| Asistente de tres pantallas | 2 pasos: apertura + 1 clic; 3 llamadas del modelo | 700 | Se detiene en la segunda pantalla. No agotó 22 s ni ocho pasos. |
| Formulario inicial | 1 apertura, ningún clic | 586 | Formulario encontrado. |
| CAPTCHA | 1 apertura, ningún clic | 600 | Ayuda humana. |
| Campo obligatorio no compatible | 1 apertura, ningún clic | 586 | Ayuda humana; sin lectura dirigida ni llenado. |

Estos tiempos son evidencia de esa simulación, no predicciones para red, modelos ni portales de producción. Los eventos operativos permiten medir la duración de cada apertura/clic y la parada resume el acumulado.

## Reproducciones acotadas (Objetivo 2)

Solo se añadieron las tres clases de página solicitadas en `tests/test_portal_step_diagnostics.py`, reutilizando `prepared()`, `RealBrowser`, el SDK y Chromium. `prepared()` admite un modelo de prueba opcional; su comportamiento por defecto no cambia. El modelo de la fixture elige referencias observadas, sin comparar palabras, nombres ni dominios. Las páginas son inventadas y no hay red real.

| Caso | Resultado del comportamiento solicitado | Razón de parada | Causa comprobada / prueba |
|---|---|---|---|
| a) Encuesta → login con opción sin cuenta → formulario | **Falla llegar al formulario; pasa la prueba de caracterización y diagnóstico.** Se conserva pendiente. | `captcha_or_unfillable`, filtro `login_required`, paso 2 | Hay password visible y la barrera existente impide otro clic, incluso con una referencia válida. El primer clic sin evaluar sí se ejecuta. No se aplicó un arreglo dudoso. |
| c) Formulario visible al entrar | **Pasa.** | `form_found`, paso 1 | Se detectan RFC y Ticket sin navegar ni escribir. Se verifica que ambos inputs siguen vacíos. |
| f) CAPTCHA o control obligatorio no compatible | **Pasa en ambas variantes después del arreglo; la variante no compatible fallaba antes.** | `captcha_or_unfillable`; filtro `captcha` o `unsupported_required` | CAPTCHA reconocido por la señal existente, o input de archivo obligatorio (imagen de verificación) que no puede completar. Telegram pide ayuda; no ofrece llenar/continuar facturación, no hay clics, lecturas dirigidas ni envíos. |

**Importante:** el caso a no se cuenta como compatibilidad resuelta con asistentes. Su prueba afirma la parada segura y documenta el impedimento, no que haya llegado al formulario. No se usó `xfail`, `skip` ni se eliminó una prueba para presentar todo como resuelto.

### Arreglo separado y evidencia antes/después

1. `8193ac4`: instrumentación, mensajes y reproducciones. Resultado de las nuevas pruebas: **14 aprobadas, 1 fallida**, en 11,48 s. Falla exacta: `tests/test_portal_step_diagnostics.py::test_verification_requires_human_help_and_never_fills_or_clicks[unfillable]`. El mensaje era «Formulario encontrado» aunque el snapshot ya contenía `unsupported_required`.
2. `ee38ba2`: arreglo general, separado. `public_form_ready()` ahora excluye controles obligatorios no compatibles; la selección automática, la herramienta y el servicio no continúan en ese estado y Telegram no ofrece continuar con IA. No depende del tipo de portal, de una etiqueta, de un dominio o de una frase. Resultado focalizado: **25 aprobadas en 19,01 s**, incluidas privacidad, significados de campos, ciclo de revisión y guarda de ausencia de lógica por comercio.

Se conservaron los permisos por dominio, alcance y propietarios, cuotas, retención, cifrado, dependencias y despliegue. No se modifica `portal_permissions.py`, `policy.py` ni `store.py`. Los metadatos del worker son opcionales en el cliente nuevo para admitir respuestas antiguas; para obtener todos los nombres y descartes deben actualizarse agente y navegador juntos. No se ejecutó esa actualización en el VPS.

## Validación completa y reproducción

**Suite completa final: 919 aprobadas, 0 fallos, 0 errores y 0 omitidas; 402,15 s (6 min 42 s).** JUnit verificado. Son los 904 casos de la base y 15 casos nuevos. Ninguna prueba intermitente falló en esta corrida; no se ignoró ni retiró ninguna. Esto no convierte el caso a en compatibilidad resuelta: su prueba de caracterización verifica la parada segura descrita en la tabla. XML: `.local/pasos-suite.xml`; log: `.local/pasos-suite.log` (ignorados por Git). Evidencias focalizadas temporales: `/tmp/pasos-antes-correccion.log`, `/tmp/pasos-despues-correccion.log`.

```sh
export UV_CACHE_DIR=/workspace/.cache/uv
uv sync --frozen --group browser
uv run --frozen --group browser pytest -q -s tests/test_portal_step_diagnostics.py
uv run --frozen --group browser pytest -q --junitxml=.local/pasos-suite.xml
```

## Qué quedó sin resolver

- El asistente local no atraviesa la pantalla de login; no se añadió una excepción insegura ni una regla para «continuar sin cuenta».
- La causa del portal real sigue sin confirmarse. Los eventos nuevos deben recogerse cuando el dueño despliegue y repita su prueba; esta rama no se desplegó.
- No se sabe cómo se comportarán páginas o verificaciones visuales que no expongan las señales de accesibilidad/CAPTCHA conocidas. No se añadieron fixtures de otros tipos, ni OCR para resolver verificaciones, ni heurísticas para saltarlas.
- No se emitieron facturas, enviaron correos, consultó SAT ni usaron credenciales reales. Solo se subirá la rama pedida; sin PR ni cambios a `main`.
