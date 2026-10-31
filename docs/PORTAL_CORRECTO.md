# Sesión 3A: localizar y observar el formulario

Rama: `portal-correcto`. Base: `eae6298` de `mejoras-flujo`.
Estado: implementación local; cierre pendiente de autorización de pruebas y recuperación del entorno. La revisión pública llega hasta los campos del formulario. Las regresiones de navegación ejecutadas comprueban controles vacíos y ausencia de solicitudes de factura.

## Qué cambió

1. La extracción conserva todas las direcciones impresas, con su propia frase y su propósito. La intención se evalúa por significado; una dirección de sucursal o encuesta no desplaza a la indicada para obtener el comprobante. La lista conserva también las direcciones descartadas y su contexto. Los documentos guardados con el formato anterior siguen siendo aceptados.
2. La dirección candidata permanece separada de `billing_url` mientras se comprueba. Un GET público con DNS validado, IP fijada y TLS debe recibir respuesta satisfactoria antes de pasar a `awaiting_form`. La comprobación no analiza campos, no sigue redirecciones ni envía referencias de compra. Los fallos quedan registrados con motivo.
3. El respaldo usa este orden: dirección impresa válida, portal guardado del mismo comercio y RFC emisor, web oficial impresa, búsqueda oficial con `discover_official_site`, correo expresamente identificado para facturar y asistencia manual. La búsqueda conserva la exigencia de `web_search` real, fuentes, evidencia y ausencia de ambigüedad. No construye dominios. Una memoria ambigua no se elige al azar.
4. El modelo recibe el contexto visible y referencias de enlaces, botones, imágenes con descripción, menús, pestañas, opciones y Shadow DOM. Interpreta la intención y puede seguir controles que los atajos de texto no reconocen. Después de cada paso recibe una observación nueva. Los selectores públicos pueden iniciar navegación; ahora se espera ese cambio con un plazo corto antes de observar para evitar falsos formularios vacíos.
5. Un permiso de recorrido mantiene el presupuesto del recorrido; cada dominio nuevo requiere un permiso explícito. Esto cubre documentos, iframes, redirecciones y recursos auxiliares. Telegram muestra la ruta, por ejemplo `origen → destino`, antes de acceder al dominio nuevo. Los registros, propietarios, caducidad y consumo único del permiso existente se conservan. Las rutas paralelas de recursos se muestran por separado.
6. Las etiquetas desconocidas pueden interpretarse mediante `field_meanings` en la misma herramienta de observación. Solo acepta referencias de campos de la observación vigente y categorías admitidas. No acepta valores ni permite cambiar el significado de un campo ya reconocido. Los campos de compra se leen después según el formulario; Caja queda separada de tienda, folio y terminal bancaria. Los valores ausentes se solicitan al usuario.
7. Un callejón sin salida muestra dónde se detuvo la revisión y ofrece escribir la dirección. Se mantienen los límites de pasos, peticiones, bytes, herramientas, turnos y tokens. No se abre una cadena automática de nuevos trabajos para seguir reintentando.
8. Se retiraron la plantilla de un comercio y su dominio por defecto. Una prueba analiza los archivos de `backend/app` y `backend/browser_worker` para impedir dominios comerciales fijos y los nombres de los ejemplos conocidos. Los únicos hosts técnicos admitidos por esa prueba son las integraciones existentes y los namespaces/proxy de seguridad, no reglas de comercios.

## Archivos de producción

| Archivos | Función |
|---|---|
| `billing/extraction.py`, `billing/models.py`, `billing/receipt.py` | Direcciones con contexto, QR y campos independientes. |
| `billing/portal_probe.py`, `billing/portal_discovery.py`, `billing/portal_memory.py`, `billing/api.py` | Comprobación de respuesta, respaldo y aislamiento de la memoria. |
| `browser_worker/engine.py`, `billing/browser.py` | Controles observados, referencias, contexto, selectores y barrera por dominio. |
| `billing/tools.py`, `agent_service.py` | Instrucciones generales e interpretación opcional de etiquetas observadas. |
| `billing/portal_preparation.py`, `billing/service.py` | Metadatos de campos, etapas y permisos antes de nuevos destinos. |
| `billing/telegram.py`, `billing/telegram_discovery.py` | Cadena de dominios, dirección manual y continuación del flujo. |
| `billing/portal_inspection.py`, `config.py` | Inspección estática sin plantilla ni dominio comercial predeterminado. |

Los archivos `billing/` están bajo `backend/app/`. Se modificaron fixtures de pruebas para declarar permisos previos explícitos cuando el objetivo de esas pruebas es extracción, corrección o llenado aislado. No se añadió una excepción de permisos al código de producción ni un bypass global en las pruebas. Las pruebas nuevas de navegación empiezan sin esos permisos y verifican la pausa.

## Páginas simuladas inventadas

Además de la regresión integrada de las dos direcciones y el hub con marcas:

| Página | Estructura y decisión |
|---|---|
| Pie en francés | Un enlace «Justificatif fiscal» está en el pie de página. |
| Menú desplegable | «Opciones» revela un segundo enlace a documentos de consumo. |
| Botón con icono | Un SVG descrito y el contexto de una sección permiten abrir un panel oculto. |
| Tarjeta con imagen | La descripción de una imagen identifica el acceso dentro de una tarjeta. |
| Pestaña | Un control `role=tab` descubre el formulario dentro de la misma página. |
| Recorrido en alemán | «Steuerbeleg» lleva a una página intermedia y luego «Weiter zum Beleg» al formulario. |
| Selector nativo | Elegir una unidad de un `select` inicia navegación mediante `onchange`. |
| Shadow DOM | El acceso está dentro de un componente con Shadow DOM abierto. |
| Callejón sin salida | Una página corporativa sin accesos útiles termina y ofrece una dirección manual. |
| Formulario con etiquetas alemanas | Los significados se asocian a las referencias observadas, sin escribir valores. |
| Cambio de dominio | El destino y los iframes/redirecciones permanecen sin consultar hasta el permiso. |
| Recurso funcional externo | Un script permanece sin consultar; solo se carga tras conceder su dominio explícitamente. |

Los transportes y el modelo son dobles locales; Chromium y el Runner del SDK son reales. Estas pruebas verifican los contratos y las acciones, no la precisión de un modelo real ante cualquier diseño o fotografía.

## Validación y autorizaciones pendientes

- Dependencias fijadas: `uv sync --frozen --group browser`.
- Línea base: **838 aprobadas, 0 fallidas, 0 omitidas; 450,79 segundos**.
- Conjunto nuevo ejecutado antes de los últimos cierres: **44 aprobadas en 52,06 segundos**.
- Última corrida completa, iniciada en `c5a5014`: **866 aprobadas, 15 fallidas, 0 omitidas; 500,87 segundos**. Dos fallos eran regresiones reales de pestañas Bootstrap y se corrigieron en `6fc35f4`; sus dos variantes pasaron después dentro de una selección de **25 pruebas aprobadas en 14,00 segundos**. Las otras 13 pruebas codifican las expectativas incompatibles enumeradas abajo.
- La siguiente corrida completa, iniciada en `6fc35f4`, se detuvo por saturación del entorno: **335 aprobadas y 88 fallidas al interrumpirla a los 164,99 segundos**. No es una medición válida de regresiones del producto: Chromium dejó de poder crear procesos e hilos (`Cannot fork`, `pthread_create: Resource temporarily unavailable`). No se presenta como suite completa.
- El diagnóstico encontró **18.473 procesos zombis** de Chromium/crashpad, hijos del PID 1 del contenedor de pruebas. Se intentó aislar el proceso de pruebas y recoger solo sus propios hijos huérfanos mediante un ejecutor temporal fuera del repositorio. Chromium continuó sin poder arrancar. No se modificaron límites del agente ni el VPS.
- En el estado actual pasan **21 pruebas de contratos sin Chromium en 6,67 segundos**. La selección excluye explícitamente un caso de navegador; no se convirtió ninguna prueba en `skip`. La recolección completa confirma **885 casos: los 838 originales y 47 nuevos**, sin borrar casos anteriores.
- La última regresión añadida, `test_form_associated_selects_and_plain_form_buttons_are_not_navigation_actions`, queda **NO VERIFICADA en Chromium** por ese bloqueo de arranque. La sintaxis Python y JavaScript del cambio sí se comprobó. También falta repetir toda la suite sobre el HEAD final en un entorno limpio, incluyendo la recuperación de checkpoints con permisos explícitos.

No se han cambiado aserciones existentes. Se comparan sus AST contra `eae6298`, además de revisar el diff. Hay tres autorizaciones solicitadas antes de modificar expectativas incompatibles:

1. Pruebas que esperan que un único permiso abra proveedores o recursos de otro dominio automáticamente. La regla actual exige el permiso antes de acceder.
2. Pruebas que esperan la plantilla del comercio cuando fallan DNS, redirección o conexión. La regla actual exige campos vacíos cuando no hay formulario verificado.
3. Una prueba que exige sustituir automáticamente una dirección impresa válida por un portal guardado. La prioridad actual conserva la impresa y usa la memoria como respaldo.

Las comprobaciones de seguridad y las aserciones existentes se mantienen mientras llega esa autorización. Se verificó su igualdad por AST frente a `eae6298`. El resultado final de la suite debe registrarse después de resolver los conflictos y recuperar el entorno, sin ocultarlos como omisiones.

Pruebas incompatibles identificadas en la corrida completa:

| Archivo | Casos | Expectativa anterior |
|---|---:|---|
| `test_adaptive_browser.py` | 1 | No volver a pedir permiso para recursos externos. |
| `test_portal_inspection.py` | 6 | Devolver una plantilla pese a DNS privado/mixto, redirección rechazada o conexión fallida. |
| `test_portal_memory.py` | 1 | Sustituir automáticamente la dirección impresa válida por la guardada. |
| `test_portal_permissions.py` | 1 | Abrir un proveedor con el permiso amplio inicial. |
| `test_public_portal_route.py` | 4 | Cruzar dominios de páginas/recursos y ampliar automáticamente la lista de dominios. |

La nueva comprobación de checkpoints rechaza permisos implícitos heredados; las expectativas de recuperación que dependían de ellos también deben revisarse tras obtener autorización. No se han dado por aprobadas ni por verificadas.

## Commits de implementación

- `4b02134`: direcciones con contexto y comprobación antes de anunciar el portal.
- `a6f8523`: contexto QR, aislamiento de memoria y estado de dirección candidata.
- `f9e022a`: Caja independiente y solicitud de datos ausentes.
- `996a34f`: interpretación de etiquetas observadas sin escribir valores.
- `d143584`: controles adaptables y permisos explícitos por dominio.
- `c5a5014`: retirada de la plantilla y guarda de dominios comerciales fijos.
- `6fc35f4`: selecciones guardadas comprobadas, checkpoints y redirecciones estáticas protegidos; recuperación de pestañas.
- `6a88d5d`: exclusión de controles asociados a formularios de las acciones de navegación pública.

La rama y los commits son locales. No se publicó una rama con la suite fallando ni se ejecutó un despliegue.

## Límites y casos que requieren ayuda

- Se conservan los 8 pasos por sesión —apertura y hasta 7 acciones—, 12 dominios, 60 peticiones por página y 240 por recorrido; también 4 MiB por respuesta, 8 MiB por página y 24 MiB por recorrido. El avance automático sigue limitado a 22 segundos. Los límites de modelo, herramientas y cuotas permanecen iguales.
- La entrada comprueba como máximo cuatro direcciones distintas en una búsqueda, con hasta cinco segundos por comprobación. No sigue redirecciones en esa comprobación ni repite la misma dirección dentro de ella. Al agotar ese presupuesto solicita una dirección manual antes de pagar otra búsqueda.
- CAPTCHA, login, destinos privados, HTTPS inválido, páginas que solo funcionan por HTTP y navegación que exige enviar un formulario quedan fuera del recorrido público seguro. Una marca que solo se pueda seleccionar mediante POST requiere asistencia.
- La navegación recibe descripciones, títulos, nombres de imagen, texto y estructura accesible. No se añadió reconocimiento visual de píxeles de la página. Un lienzo o una imagen sin metadatos ni contexto suficiente no se adivina.
- Menús inaccesibles, Shadow DOM cerrado, componentes incompatibles o recorridos que excedan los presupuestos pueden necesitar una dirección escrita por el usuario. No se garantiza llegar a todas las páginas web.
- La interpretación semántica de campos queda ligada a la observación vigente. Renovar el DOM exige verificar de nuevo sus referencias y significados. Esta sesión no valida el posterior llenado o envío de esos campos traducidos.
- La guarda estática detecta dominios comerciales nuevos y nombres de ejemplos conocidos; ningún análisis estático puede identificar por sí solo todos los nombres de empresas posibles. El recorrido de producción obtiene sus marcas y destinos de datos y controles observados.

Se comprobaron por diff/AST los archivos protegidos, SafetyHooks, execute, token_reservation, clasificación de cuotas y todas las aserciones existentes: permanecen intactos. Solo cambia en Settings la lista de páginas de inspección predeterminada, que queda vacía.

No se verificaron sitios reales, OCR real, emisiones, correo, SAT, Docker en el VPS ni GitHub Actions remotos. No se realizaron despliegues ni envíos reales. Se conserva `main`.
