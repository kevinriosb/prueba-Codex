# Auditoría del recorrido público de facturación

El error «Se alcanzó el límite de tamaño del recorrido» apareció después de la
corrección del número de peticiones. La revisión anterior no había cubierto
suficientemente el peso de recursos repetidos. Esta auditoría revisa el código
del recorrido completo y reproduce fallos antes de corregirlos. No certifica
que cualquier portal pueda automatizarse ni sustituye la comprobación en el VPS.

## Alcance revisado

Se revisaron el transporte de `policy.py`, la vida de las sesiones, interceptación,
contabilidad, cookies, observación del DOM y acciones de `engine.py`, el contrato
del cliente `billing/browser.py`, permisos/recuperación en `billing/service.py`,
la selección genérica en `portal_navigation.py`, la memoria en `portal_memory.py`,
los plazos/metadatos de `tools.py` y el resultado/diagnóstico de `telegram.py`.
También se verificaron los puntos de entrada de las imágenes y Compose. No es
una auditoría exhaustiva de todas las funcionalidades ajenas a navegación.

## Fallos reproducidos antes de la corrección

| Fallo | Resultado corregido | Prueba de regresión |
| --- | --- | --- |
| Bibliotecas repetidas se descargaban y cargaban al total otra vez en cada página | Caché HTTP efímera por contexto, con límites y sin compartir usuarios/cookies | `test_repeated_public_assets_do_not_exhaust_route_before_final_form` |
| Un bundle normal mayor de 1 MiB se descartaba sin explicar el recurso | Tope de 4 MiB, lectura incremental y diagnóstico explícito | `test_normal_application_bundle_over_one_megabyte_can_render_form` |
| La página sintética de redirección perdía cookies de sesión del documento | Se conservan las cookies en el salto documental observado | `test_document_redirect_preserves_public_session_cookies_on_original_origin` |
| Varias cabeceras Set-Cookie se confundían como una sola | Se conservan separadas, incluidas fechas Expires con coma | `test_fetcher_keeps_multiple_set_cookie_headers_separate` |
| Un enlace cambiado podía ejecutar su destino anterior | Se revalida la firma del control antes de consumir la acción | `test_changed_link_is_refreshed_instead_of_executing_its_old_destination` |
| Enlaces GET dentro de un formulario externo quedaban excluidos | Se permite navegar al href observado sin ejecutar un envío | `test_get_link_inside_outer_form_is_navigation_without_executing_submit_handler` |
| Un CAPTCHA oculto detenía páginas utilizables | Solo un control CAPTCHA visible exige intervención | `test_hidden_captcha_marker_does_not_block_public_form_observation` |
| Dos hostnames en la misma IP podían reutilizar una conexión TLS validada solo para el primero | Se desactiva la reutilización de conexiones; cada hostname recibe su handshake verificado | `test_two_hosts_on_same_pinned_ip_each_get_their_own_tls_handshake` |

Las primeras siete regresiones están en `tests/test_browser_resource_audit.py`;
la octava usa el transporte HTTP real y un backend de red simulado en
`tests/test_browser_tls_connections.py`. Se observaron sus fallos antes del cambio.

## Recursos y recuperación

La solución combina caché, presupuestos separados, cancelación incremental y
diagnóstico. El recorrido autorizado admite 60 solicitudes por página y 240
totales, 4 MiB por respuesta, 8 MiB por página y 24 MiB de red por recorrido.
Los bytes se miden descomprimidos. El límite total de la API antigua sigue siendo
6 MiB. El plazo de ocho segundos por GET incluye DNS y lectura; evita que una
respuesta que gotea datos mantenga la descarga viva indefinidamente.

La caché de 8 MiB vive solo dentro del contexto, hasta 180 segundos. Solo admite
scripts/estilos con max-age explícito; respeta Cookie/Vary y rechaza private,
no-cache, no-store y Set-Cookie. Los documentos y XHR no se cachean. Un acierto
sigue contando como petición y bytes de página, sin duplicar el total de red.
Los recursos pendientes de una página anterior no agotan por error el contador
de la nueva. Se vacía la caché al cerrar el contexto.

El presupuesto de IA comienza antes de abrir la primera página. La recuperación
desde el último enlace público estable incluye agotamiento de bytes y de
peticiones. Solo un trabajo nuevo autorizado puede reabrir una sesión agotada
una vez; el modelo no reinicia sus límites dentro del mismo trabajo. Los permisos,
la titularidad y las cuotas siguen verificándose.

## Otros casos cubiertos

Las regresiones adicionales comprueban el corte de respuestas durante streaming,
el límite de página y el global, caché LRU/TTL/Vary y cambios de cookies,
cabeceras/CORS, errores DNS/TLS/HTTP sin datos privados, respuesta incompatible
entre contenedores, sesión incorrecta y rechazo de un navegador fuera del modo
público. También cubren iframes visibles/srcdoc, enlaces en Shadow DOM abierto,
expansiones Bootstrap, catálogos largos, controles modificados y reducción de
observaciones grandes antes de superar el contrato de 64 KiB.

Las observaciones incompletas se marcan como parciales; no se presentan como una
lectura exhaustiva. Un login visible o CAPTCHA detiene el avance. Un destino PDF
o JSON se identifica como documento incompatible con esta fase HTML.

## Diagnóstico y verificación de producción

Telegram ofrece **Ver diagnóstico (sin IA)** cuando hay bloqueo, incidencias de
recursos o una vista parcial. Lee el diagnóstico guardado del ticket del usuario;
no abre páginas ni consume tokens. Muestra bytes/peticiones, reutilizaciones,
solicitudes de envío omitidas y causas acotadas. No muestra cookies, cabeceras,
valores de formularios, cuerpos de respuestas ni trazas privadas.

Este único comando hace una prueba pública nueva con Chromium desde el VPS:

```bash
cd /opt/lilyapp-agent
docker compose -f compose.hostinger.yaml -f compose.browser.yaml exec -T browser \
  python - --url "https://alsea.interfactura.com/RegistroDocumento.aspx" \
  --merchant Dominos < scripts/diagnose_portal.py
```

La URL y marca son los datos del ejemplo reportado, no reglas incorporadas. Para
otro ticket se cambian ambos argumentos. El diagnóstico sigue únicamente acciones
observadas mediante la misma selección genérica del servicio. No usa IA, perfiles,
CSF, cuotas ni datos de compra; no rellena formularios ni emite facturas. Imprime
por paso etiquetas y códigos acotados, sin valores, cookies ni consultas URL.
Salida 0: formulario observado; 2: bloqueo o elección pendiente; 1: error del
diagnóstico. El resultado 0 no significa que se haya validado una compra.

El proxy del entorno de desarrollo rechaza la conexión a Interfactura (CONNECT
403). No se desactivó TLS ni se evitó ese proxy. La navegación del portal real
debe verificarse en el VPS después de reconstruir **agent y browser**. No hace
falta borrar datos ni volver a subir la CSF o el ticket.

## Límites que siguen existiendo

Esta fase navega por contenido público GET y se detiene ante el formulario.
No rellena datos fiscales ni solicita facturas. Una redirección de documento
con cookies es compatible; un recurso auxiliar con Set-Cookie en un salto
intermedio se rechaza como `redirect_cookie` para evitar instalar la cookie bajo
otro dominio. Una página que dependa de ese mecanismo puede requerir adaptación.

CAPTCHA, acceso privado, POST necesario para avanzar, canvas, Shadow DOM cerrado,
controles ambiguos o sin etiquetas y páginas que excedan presupuestos pueden
necesitar intervención. Se omiten imágenes, fuentes y medición. No hay reglas
especiales para Domino’s, Alsea o REA ni garantía de compatibilidad universal.

## Evidencia de esta entrega

Suite completa local: **626 pruebas aprobadas**, 191,13 segundos. Incluye Chromium
y SDK reales con páginas/modelos simulados; no usa OpenAI, Gmail o SAT reales.
Ruff (F821/F822/F823/E9), compilación y comprobación del diff aprobados. Ambas
imágenes finales se construyeron y pasaron la prueba del contenedor y del stack
Compose: salud, autenticación interna, rechazo de archivos locales, UID sin
privilegios y ejecución del diagnóstico por stdin con las dependencias de
producción. Los contenedores y volúmenes ficticios se retiraron al terminar.
