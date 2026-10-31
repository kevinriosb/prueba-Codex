# Corrección de prueba intermitente y revisión no iniciada

Fecha: 2026-10-31. Rama: `fix-prueba-intermitente-y-revision-vencida`.
Base: `main` en `e1ba7dc`, que incluye `8b103b4`. Sin despliegue ni PR.

## A. Preparación de la foto de un campo

La prueba de foto del portal cambiaba el transporte de páginas a `PortalPages()` inmediatamente después de `prepared()`. Pero ese helper solo garantizaba haber ejecutado el botón `wa`, no haber encontrado el formulario ni leído el ticket. Si la revisión quedaba pausada, el siguiente botón `lv` abría el portal del transporte nuevo (RFC y Ticket), con todos los datos de compra todavía vacíos. Ya no existía el campo Tienda cuyo recorte pretendía probar.

Se reprodujo **determinísticamente** el estado reportado: avanzando exclusivamente el reloj del presupuesto de navegación 23 segundos durante la apertura, `prepared()` devuelve `awaiting_form`, sin total, folio, tienda ni fecha; solo se llamó `open`, sin `act` ni extracción dirigida. El flujo es síncrono: no queda una tarea pendiente que vaya a completar la navegación después. Es la pausa prevista por el plazo de 22 segundos, no una autorización perdida ni un envío de datos. La nueva prueba conserva este comportamiento de producción.

La fixture de esta prueba de datos ahora completa explícitamente la ruta pública observada, con las mismas referencias y permisos, hasta afirmar que hay formulario utilizable. Espera el input visible en Chromium y confirma `data_read` y los cuatro datos antes de cambiar el transporte. No añade turnos de IA ni cambia plazos del producto; si hay bloqueo, falta de acción o error, falla en esa precondición. Las aserciones de foto, perfil, solicitud y valor manual permanecen. Se retiró `DIAGNOSTICO2` y sus variables temporales.

**Límite de la conclusión:** las 20 repeticiones aisladas y las 20 con carga del código anterior pasaron. El fallo publicado de CI confirma la precondición ausente, pero no contiene el tiempo ni el error de la apertura. El agotamiento del plazo reproduce el mismo estado y explica un camino concreto; no prueba que fuera el disparador exacto de aquellas ejecuciones. Se añade el código de diagnóstico `browser_route_time_limit` para distinguirlo en futuras corridas. No se afirma haber encontrado una carrera real del producto.

Pruebas: `tests/test_preparation_readiness.py` comprueba tanto la pausa original como la preparación completa con ese mismo reloj; la prueba original de la foto conserva sus dos variantes.

## A secundaria. Tormenta de peticiones auxiliares

`DOMContentLoaded` y `load` no esperan a los `fetch` de la página. Por eso la primera observación podía llegar antes de que las 65 solicitudes hubieran contado para el límite. Se demuestra con una barrera que mantiene esas peticiones pendientes: `open()` devuelve cero escrituras omitidas y sin bloqueo; tras liberarla, se alcanza el límite.

La página simulada marca `stormDone` únicamente cuando termina su `Promise.all`. La prueba espera esa señal y vuelve a observar antes de comprobar **las mismas aserciones** de bloqueo, código `page_request_limit`, al menos 60 escrituras omitidas y ninguna petición privada reenviada. Se ejecutan las variantes normal y con barrera. No se cambian los límites ni el código de red del navegador.

## B. «La revisión venció»

Se añadieron primero registros JSON `portal_flow` en `personal_agent.portal`. Solo incluyen herramienta, fase, código de error y los booleanos `scope_changed`, `browser_review_exists`, `permission_current`. Cubren apertura, observación, navegación, `act` e interpretación de campos. No incluyen identificadores, RFC, correo, texto de ticket o página, destinos, parámetros, argumentos, excepciones, tokens ni claves.

La reproducción sigue el orden de producción: dirección impresa candidata con `billing_url=None` → comprobación del host → dirección validada → permiso de ruta → Runner real del SDK → primera llamada `portal_browser_observe`. Usa Chromium real, modelo y transporte simulados. Se prueban una lista vacía `field_meanings=[]` y significados sin observación previa.

**Defecto confirmado (hipótesis b):** antes, cualquier `field_meanings` distinto de `None`, incluso `[]`, entraba a `describe_fields()`. Este consultaba `browser_review()` antes de abrir ninguna página y producía `404 browser_session`: no existía revisión, el permiso estaba vigente y el alcance no había cambiado. Las dos regresiones fallaron antes de la corrección y los registros afirmaron la fase `describe_fields`. Después llegan al formulario con una sola llamada de modelo, sin llenar ni enviar.

Ahora una lista vacía observa normalmente. Los significados no vacíos solo se aplican si hay una observación previa identificada y una revisión disponible. En caso contrario se obtiene primero una observación y se devuelven sus referencias; los significados adelantados no se aplican a ciegas. Las comprobaciones de observación obsoleta, referencia ajena, campo ya reconocido y referencia repetida se mantienen.

El mensaje del servicio distingue revisión no disponible de cambio de perfil/portal; no atribuye caducidad a una revisión que nunca existió. La doble pulsación del mismo botón durante su ejecución responde «Ya estoy revisando esta solicitud. Espera el resultado.» usando el bloqueo por usuario existente. Una prueba con eventos mantiene la primera ejecución en curso, envía otra actualización con el mismo botón y comprueba una sola corrida SDK y una sola apertura. Se limpia el seguimiento de callbacks incluso al fallar.

**Lo que sigue sin confirmarse:** no están disponibles los argumentos de las dos corridas históricas de producción. El defecto b reproduce el síntoma, pero no se atribuye con certeza aquel incidente a ese defecto. En la reproducción no cambia el alcance (a) ni se consulta una sesión cerrada (c). Si vuelve a ocurrir tras el despliegue autorizado por el dueño, los registros permitirán identificar la fase sin datos personales. No se accedió al VPS ni se probó el ticket real.

Una regresión adicional cambia el portal después de guardar una revisión: las concesiones anteriores no valen para el nuevo alcance, el checkpoint no se retoma y la herramienta pide permiso humano sin otra apertura. Los registros detectan el cambio sin mostrar el dominio ni un parámetro de prueba marcado como secreto.

## Qué no cambió

Sin lógica por comercio; sin cambios de permisos, alcance/digest, documentos o iframes admitidos, presupuestos, cuotas, aprobaciones, AES-GCM, secretos, dependencias, Docker ni workflow. No se implementa S3 (retención y entrega), SAT, calidad de foto, multiusuario u otra sesión futura. Sin envíos reales, despliegue, fusión a `main`, PR ni cambios de configuración del entorno.

## Evidencia y reproducción

Resultados de estabilidad (20 procesos de pytest independientes por bucle; sin `pytest-repeat`):

| Código de las pruebas | Entorno | Foto `[portal]` | Tormenta auxiliar |
|---|---|---:|---:|
| Antes de corregir las fixtures | Normal | 20/20 | 20/20 |
| Antes de corregir las fixtures | Una CPU compartida con cálculo continuo | 20/20 | 20/20 |
| Fixtures corregidas | Normal | 20/20 | 40/40 (20 por variante) |
| Fixtures corregidas | Una CPU compartida con cálculo continuo | 20/20 | 40/40 (20 por variante) |

**Suite final: 904 aprobadas, 0 fallos, 0 errores, 0 omitidas; 396,58 s (6 min 36 s).** JUnit verificado en `.local/fix-evidence/suite-final.xml`. CI remoto: el workflow existente «Pruebas del agente» se ejecuta al subir la rama; su estado debe comprobarse en GitHub sobre el commit subido. Regresiones focalizadas: **11 aprobadas en 11,01 s**, sin omisiones. La suite intermedia, anterior a las últimas regresiones, pasó **899/899 en 411,76 s**; no se presenta como suite del cambio final. La recolección final contiene **904 casos** (los 897 originales y siete casos adicionales). Por comparación AST se conservan las 42 expresiones de aserción del archivo de fotos y las 30 del archivo de selectores, además de la nueva aserción de la barrera.

Logs y XML locales: `.local/fix-evidence/` (ignorados por Git; datos ficticios).

```sh
export UV_CACHE_DIR=/workspace/.cache/uv
uv sync --frozen --group browser
uv run --frozen --group browser pytest -q tests/test_preparation_readiness.py tests/test_portal_review_lifecycle.py tests/test_portal_field_meanings.py
uv run --frozen --group browser pytest -q --junitxml=.local/fix-evidence/suite-final.xml

for attempt in $(seq 1 20); do
  uv run --frozen --group browser pytest -q \
    'tests/test_ambiguous_receipt_photo.py::test_requested_field_photo_keeps_its_recorded_expectation_and_applies_without_a_ticket_choice[portal]' \
    tests/test_brand_selector_regressions.py::test_auxiliary_write_retry_storm_still_hits_page_request_limit || exit 1
done
```

Para carga de CPU en Linux, ejecutar el bucle anterior con `taskset -c 0` y mantener otro proceso de cálculo en ese mismo núcleo; finalizar solo ese proceso propio al terminar. Se empleó un `python -c 'while True: pass'` con PID guardado y `trap` para retirarlo. No se modificaron límites de la máquina ni procesos ajenos.
