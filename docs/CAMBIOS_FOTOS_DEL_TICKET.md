# Fotos del ticket: calidad, direcciones e identidad

Fecha: 2026-10-31. Rama `fotos-del-ticket`, desde `origin/main` en
`13a6ec7` (incluye el ciclo de pasos). No se modificó `main`, no se abrió
PR y no se desplegó ni se enviaron formularios reales.

## Lo encontrado

La lectura inicial no tenía un resultado estructurado para zonas ilegibles
ni confianza por campo. Por eso una dirección omitida podía acabar en el
mensaje de búsqueda oficial sin explicar la omisión. No es posible saber,
solo con las evidencias históricas, si la IA omitió la dirección, la clasificó
de otro modo o si falló su comprobación. Ahora esas etapas son distinguibles.

`BillingService.analyze_ticket` solo utilizaba el QR cuando faltaba la URL
visual. `PortalDiscovery.find/checked` conservaba algunos errores, pero no
registraba cada dirección con propósito y resultado. El plazo de comprobación
sigue siendo de cinco segundos y no se siguen redirecciones sin permiso.

En Telegram, una foto nueva con datos básicos ya completos llegaba a
`_choose_document` y se creaba otra solicitud antes de leer su identidad.
Las fotos de corrección explícitas sí conservaban el ticket. La deduplicación
por identidad de `analyze_ticket` exigía una combinación completa de datos
y ocurría después de consumir el análisis; no cubría bien un acercamiento.
Los rechazos por el tope ya se comprobaban antes de insertar un intento:
se conserva esa propiedad y se prueba expresamente.

## Lectura y legibilidad

El esquema y las instrucciones de la primera lectura incorporan:

- `photo_issues`: zona (arriba, centro, abajo o completa), sujeto (dirección,
  QR o datos de compra) y motivo (pequeño, borroso, cortado o parcial).
- `field_confidence`: alta, media, baja o ilegible para cada campo reportado.
  La ausencia de un indicador no se convierte artificialmente en confianza alta.
- Subtotal, IVA, propina y totales con/sin propina por separado, solo cuando
  sus etiquetas e importes se leen. No se calculan valores faltantes ni se
  identifica arbitrariamente un número de orden como folio del portal.

Telegram traduce una zona reportada en una petición concreta, por ejemplo
«Acércate a la parte de abajo, donde está la dirección de facturación».
Si falta esa dirección y se reportó ilegible, se pide otra foto antes de
seguir pagando búsquedas oficiales. Si no se reportó una zona, se explica
que la lectura no transcribió una dirección; no se afirma que el ticket no
la tenga ni se inventa dónde debería estar.

Subtotal + IVA se compara con el total usando decimales y tolerancia de
dos centavos. Si no coincide se marca **revisar**, sin corregir el importe.
Esto también puede requerir revisión cuando hay descuentos/cupones: el
sistema no inventa su tratamiento. La propina no se suma ni resta para
completar un valor ausente. Para un campo observado que pide «sin propina»
se propone exclusivamente el importe explícito `total_without_tip`; si no
existe, se pide el dato.

Se muestran los campos de baja confianza y se ofrece confirmarlos. La
confirmación queda ligada al valor y la foto mostrados, con vencimiento y
propietario; un botón viejo no confirma un valor cambiado. No equivale a
autorizar llenar o enviar un formulario. Los valores dudosos no confirmados
se presentan como pendientes en la propuesta del formulario.

## Direcciones y QR

Cada dirección reportada tiene eventos `portal_flow` en
`personal_agent.portal`, herramienta `printed_address`, fase `response_check`:
propósito (`billing`, `official`, `other`, `unclear`) y resultado constante
(`valid`, `no_response`, `blocked`, `survey_filtered`, `not_billing`,
`not_literal`, `not_checked`), además de host/ruta saneados. Se distingue
una candidata todavía no comprobada de una ya comprobada; no se llama
«no respondió» a una dirección que ni siquiera se intentó.

En Telegram hay una línea por dirección no utilizada, con el motivo. Cuando
se propone abrir una dirección leída, el aviso muestra el texto de URL leído
y el host/ruta comprobados. Los parámetros y fragmentos se omiten siempre,
incluso en la vista literal. Una candidata impresa se presenta antes del
permiso inicial; no se abre primero para avisar después. Se reutilizan
las solicitudes de permiso existentes, sin conceder dominios nuevos.
El saneado también se aplica a la vista de datos del portal, la página
observada y la lista de portales guardados. El enlace usado por el navegador
y los datos guardados conservan sus parámetros: solo cambia su presentación.

Un único QR decodificado localmente con contexto de facturación tiene
prioridad sobre el texto visual. Si difieren, se avisa y se muestran ambos
destinos sin parámetros. Un QR clasificado explícitamente como otro propósito
no gana prioridad por estar en el mismo ticket que una dirección fiscal.
Varios QR sin asociación inequívoca no se eligen por orden. Se mantienen
los filtros de encuestas, la comprobación de red y las protecciones existentes.

Los logs no contienen fotos, valores de campos, RFC, nombres, folios,
importes, correos, evidencia impresa, consultas de URL ni tokens. Se sanea
el destino y se valida una lista cerrada de campos/estados. Hay una prueba
negativa que rechaza datos fiscales si un llamador intenta saltarse el saneado.

## Unión de fotos y cuota

Cuando hay solicitudes propias pendientes, la foto se guarda sin abrir
otro ticket. Se ofrece **Autorizar lectura y comparar** o confirmar
**Es el mismo ticket / Es otro**. La comparación usa datos visibles del
mismo perfil: comercio, tienda, fecha, hora, folio y total. No basta el
comercio o el total por separado. Para unir automáticamente se requieren
al menos tres coincidencias independientes, incluyendo comercio y folio,
o comercio, fecha, hora y total; sin conflictos ni apoyar la decisión en
campos de baja confianza. Si hay varias coincidencias o datos contradictorios,
se pregunta. No hay reglas por marca, dominio ni comercio.
Esto también aplica si la primera solicitud todavía no fue analizada:
al faltar datos comparables se pregunta, sin crear otra solicitud. El aviso
de lectura explica también los campos dirigidos posteriores y la búsqueda
oficial si hace falta; ninguna de esas autorizaciones aprueba un envío.

Al unir, se conserva la solicitud y sus datos; se completan los ausentes.
Un valor contradictorio no reemplaza al anterior: se señala para revisión.
Las decisiones tienen propietario, vencimiento, contexto de solicitud y
uso único. Un cambio de menú o una decisión anterior no sustituye una foto
posterior. Una indicación explícita de «otro ticket» sigue siendo respetada.

Una foto con el mismo hash reutiliza el documento y la lectura. Una lectura
para comparar reserva un lugar mientras está pendiente; si se une, el
registro pasa a `linked_*` y deja de ocupar otro análisis diario, conservando
el resultado del intento. Si se confirma como otro ticket, se reutiliza
esa lectura al crear la solicitud y se cuenta una vez.

Con el tope agotado no se llama al extractor para comparar ni se inserta
un intento rechazado. Se ofrece la unión manual. La lectura autorizada
para una foto de corrección de ese mismo ticket tampoco ocupa otro análisis
inicial. **Las lecturas ordinarias de campos por formulario conservan su
cuota y selección de campos**. Los topes configurados no cambian. Una unión
puede consumir tokens reales de OpenAI; no se presenta como gratuita.

Si ya hay un formulario privado o un envío en curso, la foto se asocia a
la solicitud sin sustituir el documento usado por ese paso, sus valores,
su sesión ni sus aprobaciones. No se hace un envío ni se reinicia el portal
por recibir una foto. Para completar un campo del paso sigue disponible
el recorte solicitado por el flujo existente.

## Validación y reproducción

Las pruebas usan el extractor falso, imágenes generadas, QR decodificados
localmente, `prepared()` y `RealBrowser`. No llaman a OpenAI ni a portales
externos. Cubren:

| Punto | Evidencia de regresión |
| --- | --- |
| Zona ilegible | Pide acercar la zona de facturación, conserva datos claros y no busca en silencio |
| Dirección no usada | Propósito/resultado en JSON y motivo en Telegram, sin parámetros ni datos fiscales |
| QR frente a texto | QR real generado localmente; discrepancia visible antes de abrir el dominio |
| Identidad de fotos | Coincidencia clara, folio contradictorio, varias candidatas, decisión de otro ticket |
| Cuota | Hash repetido, unión, lectura cacheada de «otro», rechazo sin reserva y unión manual con tope agotado |
| Importes/confianza | Tolerancia decimal, separación de propina, no inferir «sin propina», confirmación propia |
| Protocolo previo | Ambigüedad inicial nula leída al observar el formulario; foto unida sin alterar un paso privado |

Las pruebas de fotos ambiguas se adaptaron a la nueva revisión
`receipt_photo_review` y a los tres botones (consentir lectura, mismo, otro).
Conservan las aserciones de fuente intacta antes de decidir, expiración,
usuario ajeno, menú cambiado, cuota de actualizaciones, uso único y límite
de documentos. La prueba intermitente de foto solicitada no se quitó ni
se debilitó. No se modificó el worker ni el protocolo de dos autorizaciones.

Los escenarios que necesitan dos solicitudes distintas ahora confirman
«Es otro» o indican expresamente «otro ticket» antes de analizar. No se
eliminaron sus comprobaciones de aislamiento, datos conservados ni permisos.
Las pruebas de primera observación del portal ahora pulsan el permiso
después de ver la URL literal; conservan la comprobación del orden
apertura → observación y de ausencia de `browser_session`.

La primera suite completa intermedia dio **938 aprobadas y 18 fallidas**
(464,18 s). Se encontró una colisión real con el código del botón de CSF,
corregida usando `qc` para confianza. Los demás fallos correspondían a
escenarios aún preparados para crear automáticamente un ticket o arrancar
la revisión antes del aviso de dominio. Entre ellos,
`tests/test_portal_review_lifecycle.py::test_double_analysis_click_does_not_start_a_second_review`
agotó su espera porque el modelo ya no debía empezar hasta el permiso.
Se conserva esa prueba para el análisis duplicado y se añade otra carrera
controlada sobre el botón de permiso: una sola revisión y respuesta
«Ya estoy revisando». No se atribuyeron esos fallos a intermitencia.

La presentación sin parámetros también cambia dos expectativas anteriores:
la lista de portales muestra host/ruta, conservando el enlace íntegro para
navegar; y una dirección rechazada ahora debe aparecer acompañada de su
motivo, en vez de quedar oculta. En
`tests/test_portal_entry_candidates.py::test_unreachable_printed_address_is_discarded_and_official_search_really_runs`
se conserva la comprobación de destino correcto y se exige además que la
dirección fallida nunca se anuncie como «Portal». El conjunto de direcciones
pasó sus 14 pruebas (2,99 s) con esas aserciones.

Validaciones focales intermedias: 47 aprobadas en lectura/protocolo,
44 en identidad/contexto y 28 en los últimos casos de fotos/observación
(15,82 s). Dos ejecuciones completas iniciadas antes de los últimos ajustes
de metadatos y de fotos previas al primer análisis se interrumpieron para
reiniciar con todo el código final; no se usan como evidencia de estabilidad
ni se excluye ningún caso en el reinicio.
Una ejecución completa posterior dio **964 aprobadas**, cero fallos, errores
u omisiones (463,99 s). Después se completó el saneado de las vistas antiguas
de URL y su regresión; la comprobación final completa figura debajo.

Comandos desde la raíz, con Chromium instalado:

```bash
UV_CACHE_DIR=/workspace/.cache/uv uv run --frozen --group browser pytest -q tests/test_receipt_photo_quality.py tests/test_ambiguous_receipt_photo.py tests/test_receipt_conversation_recovery.py
UV_CACHE_DIR=/workspace/.cache/uv uv run --frozen --group browser pytest -q --junitxml=.local/fotos-entrega.xml
git diff --exit-code origin/main -- backend/app/billing/portal_permissions.py backend/browser_worker/policy.py backend/app/billing/store.py
```

**Suite completa final: 965 aprobadas, cero fallos, cero errores y cero
omisiones**, en 465,72 segundos (7 min 45 s). El artefacto JUnit local es
`.local/fotos-entrega.xml` (no versionado). Incluye
`tests/test_no_merchant_specific_routing.py`, el ciclo de pasos y las pruebas
intermitentes existentes; ninguna se quitó ni falló en esta ejecución.

## Qué no cambió y qué falta comprobar

No se cambiaron `policy.py`, `store.py`, permisos por dominio, retención,
cifrado, topes ni reglas por comercio. Cada formulario conserva las dos
autorizaciones separadas y el mecanismo de lectura dirigida existente.
No hay reintentos automáticos de análisis fallidos ni envíos reales.

Falta desplegar y repetir las fotos reales. Estas pruebas demuestran el
tratamiento de resultados y la trazabilidad; no miden la precisión visual
de OpenAI sobre las dos fotografías del incidente. No existe una garantía
de detectar toda letra pequeña: el reporte de legibilidad depende del
modelo. Tampoco se completa una URL cortada, se resta una propina no
identificada ni se elige entre QR ambiguos. En una sesión privada en curso,
unir una foto conserva la fuente y las aprobaciones de ese paso.
