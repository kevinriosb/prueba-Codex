# Auditoría del encargo: flujo de facturación

Fecha: 2026-10-30. Repositorio: `kevinriosb/prueba-Codex`. Rama de trabajo: `mejoras-flujo`. Código auditado: `b6c58978d81b3ba22f7cac14bdc7d5714d29277f` (comprobado con Git antes de crear este documento).

Alcance: lectura de código, ejecución de la suite y caracterizaciones temporales. Este documento es el único archivo incorporado al repositorio. No se modificaron código, configuración ni pruebas. Las referencias `archivo:línea` corresponden al código auditado; los ensayos adicionales se describen aquí, sin guardar pruebas nuevas.

**NO VERIFICADO:** configuración efectiva del VPS, disponibilidad de comercios reales, búsquedas reales de OpenAI, emisión ante un comercio real, buzones reales, respaldos existentes fuera del repositorio y recuperación de esos respaldos. La suite usa modelos/proveedores simulados; algunas integraciones ejecutan Chromium real contra páginas y transporte simulados (`tests/test_portal_submission.py:51`, `tests/test_portal_submission.py:184`). Pasar la suite no demuestra compatibilidad con cualquier página.

## 1. Línea base

Ejecutado en un checkout limpio del commit indicado, usando el entorno instalado en `.venv`:

```sh
UV_CACHE_DIR=/tmp/lilyapp-uv-cache uv sync --frozen --group browser
PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR=/tmp/lilyapp-uv-cache \
  uv run --frozen --group browser pytest -q -r a \
  --junitxml=/tmp/lilyapp-audit-baseline.xml
```

La instalación fijada terminó correctamente: `Checked 61 packages in 2ms`. Resultado capturado de pytest: **798 passed in 414.15s (0:06:54)**. El XML JUnit registró **798 pruebas, 0 fallos, 0 errores y 0 omitidas**; tiempo JUnit: 414.103 s. Artefactos temporales de esta ejecución: `/tmp/lilyapp-audit-baseline.log` y `/tmp/lilyapp-audit-baseline.xml`. Son evidencias de la ejecución local, no archivos añadidos al proyecto.

No hubo impedimento para instalar dependencias o completar la suite, por lo que se continuó con la auditoría solicitada.

## 2. Tabla completa

Los estados se refieren al requisito solicitado. En los puntos de diagnóstico, **A MEDIAS** significa que hay una implementación o protección útil junto con la limitación descrita; no significa que todo el punto sea un bug. **NO VERIFICADO** identifica aquello que no puede demostrarse en este entorno.

| ID | Estado | Resultado y evidencia |
| --- | --- | --- |
| F1 | **NO CUMPLE** | Hay validación local de formato, tamaño, PDF y corrupción, pero no una evaluación de nitidez, texto legible o resolución mínima antes de la lectura de IA (`backend/app/billing/documents.py:10`, `backend/app/billing/documents.py:29`). La decisión `readable` se examina después de `_extract`, que ya consume cuota y ejecuta el modelo (`backend/app/billing/service.py:201`, `backend/app/billing/service.py:322`, `backend/app/billing/service.py:332`). Una PNG blanca de 1×1 pasó la inspección local en el ensayo C1. La primera lectura v3 es de enrutamiento y datos básicos: no debe confundirse con la extracción genérica completa (`backend/app/billing/extraction.py:66`). |
| F2 | **A MEDIAS** | Existen URL/QR impreso, rechazo de encuestas, memoria de portales, búsqueda oficial y navegación pública; la búsqueda real está implementada mediante `WebSearchTool`, con comprobación de fuentes, y después Playwright sigue controles observados (`backend/app/billing/service.py:327`, `backend/app/billing/extraction.py:78`, `backend/app/billing/tools.py:153`). Sin embargo, la memoria exige que el ticket ya tenga `billing_url`, así que no sirve como segundo recurso cuando la nueva foto carece de URL (`backend/app/billing/portal_memory.py:92`). Además, un correo explícito provoca salida anticipada de la búsqueda oficial, sin intentar antes encontrar un portal (`backend/app/billing/portal_discovery.py:43`). No se cumple exactamente el orden pedido. CAPTCHA, login e incompatibilidades detienen el envío (`backend/browser_worker/submission.py:157`). Búsqueda y navegación contra el comercio real: **NO VERIFICADO**. |
| F3 | **A MEDIAS** | Coexisten tres métodos: `extract` genérico, `extract_routing` y `extract_fields` dirigido (`backend/app/billing/extraction.py:62`, `backend/app/billing/extraction.py:66`, `backend/app/billing/extraction.py:125`). Telegram con el extractor de producción por defecto usa v3: primero lee marca/canales y también folio, fecha, importe y otros datos básicos; después encuentra el formulario y lee los campos adicionales que este exige (`backend/app/billing/telegram.py:250`, `backend/app/billing/telegram.py:1017`, `backend/app/billing/portal_preparation.py:192`). La API web puede entrar por la extracción genérica al no marcar ese alcance (`backend/app/billing/api.py:86`, `backend/app/billing/service.py:211`). Por tanto, no es estrictamente «formulario antes de toda lectura de compra». Camino v3 comprobado en `tests/test_official_portal_discovery.py:37`; versión/configuración realmente desplegada: **NO VERIFICADO**. |
| F4 | **CUMPLE** | El perfil confirmado se guarda sin caducidad de documento y se reutiliza. El reinicio o vencimiento del PDF original no obliga a dar de alta otra vez el perfil (`backend/app/billing/service.py:291`, `backend/app/billing/telegram_profiles.py:30`, `tests/test_telegram_requests.py:68`, `tests/test_telegram_requests.py:87`, `tests/test_telegram_requests.py:122`). Si se necesita adjuntar la CSF completa y su archivo ya venció, pedir nuevamente ese archivo es una situación distinta de volver a extraer el perfil (`backend/app/billing/service.py:762`). |
| F5 | **A MEDIAS** | Hay valores ausentes, lectura de un recorte, entrada manual y confirmación del valor leído; los valores confirmados por la persona se protegen de sustituciones de IA (`backend/app/billing/telegram_submission.py:29`, `backend/app/billing/telegram_submission.py:76`, `backend/app/billing/portal_submission.py:240`, `backend/app/billing/portal_preparation.py:258`). No hay puntuación de confianza por campo: los esquemas tienen valores opcionales y un `readable` global (`backend/app/billing/models.py:92`, `backend/app/billing/models.py:134`, `backend/app/billing/extraction.py:18`). Los prompts prohíben adivinar y distinguen Cuenta/Folio/Código (`backend/app/agent_service.py:50`, `backend/app/billing/extraction.py:125`); no se encontró una sustitución deliberada universal de Cuenta por Folio. Que un modelo nunca invente un valor no puede garantizarse con estos esquemas: **NO VERIFICADO** para fotos reales. |
| F6 | **CUMPLE** en código y pruebas | El envío privado está conectado: botón nativo → revisión concreta con datos/destinos → aprobación consumida → cliente interno → worker Playwright → respuesta y archivos → validación del XML → consulta SAT autorizada (`backend/app/billing/telegram_submission.py:12`, `backend/app/billing/telegram_submission.py:65`, `backend/app/billing/portal_submission.py:81`, `backend/app/billing/portal_submission.py:104`, `backend/app/billing/browser.py:242`, `backend/browser_worker/app.py:129`, `backend/browser_worker/submission.py:194`). Valida RFC receptor, CP, régimen, uso, nombre, importe, UUID y otros atributos del XML; exige autorización SAT (`backend/app/billing/cfdi.py:16`, `backend/app/billing/service.py:981`). El correo también exige confirmación específica y reclama la aprobación antes de enviar (`backend/app/billing/telegram.py:768`, `backend/app/billing/service.py:762`). Pruebas de dos formularios, captura XML/PDF, Telegram completo, XML incorrecto, permisos, timeout y respuesta perdida: `tests/test_portal_submission.py:51`, `:184`, `:214`, `:248`, `:294`, `:305`. Emisión real en el comercio y compatibilidad universal: **NO VERIFICADO**. |
| F7 | **A MEDIAS** | «Otro ticket» explícito pausa el anterior; hay deduplicación de archivo/identidad y bloqueo de reenvíos inciertos (`backend/app/billing/telegram_receipts.py:28`, `backend/app/billing/telegram_workflow.py:198`, `backend/app/billing/service.py:335`, `backend/app/billing/portal_submission.py:111`). Pero una foto desconocida sin texto se interpreta como corrección si al activo le faltan datos; puede reemplazar su fuente sin crear otra solicitud (`backend/app/billing/telegram_receipts.py:43`, `backend/app/billing/telegram_receipts.py:54`). Reproducido en C3. La comparación posterior evita mezclar identidades incompatibles, pero no abre automáticamente el ticket nuevo (`backend/app/billing/portal_preparation.py:251`). |
| L1 | **A MEDIAS** | Reserva por defecto **83.250** frente a **250.000** tokens diarios (`backend/app/config.py:27`, `backend/app/config.py:99`). El camino de error omite `actual` aunque los hooks ya sumaron consumo; SQLite conserva `NULL` y contabiliza la reserva (`backend/app/agent_service.py:127`, `backend/app/agent_service.py:255`, `backend/app/database.py:174`, `backend/app/database.py:182`). **Tres fallos contabilizan 249.750; la cuarta ejecución se bloquea**, incluso en el ensayo con solo 60 tokens conocidos por fallo. Es una política conservadora explícita, no prueba de un gasto real de 249.750. Detalle C4. |
| L2 | **A MEDIAS** | Los conteos permiten abrir y avanzar hasta siete acciones automáticas: estas ocurren dentro de una herramienta SDK, no como siete llamadas del modelo (`backend/app/billing/tools.py:153`, `backend/browser_worker/engine.py:137`). No está garantizado que entren en los plazos superpuestos de 22/30/35 s y el HTTP de 15 s (`backend/app/billing/tools.py:159`, `backend/app/billing/tools.py:179`, `backend/app/agent_service.py:198`, `backend/app/billing/browser.py:130`). `tool_timeout_seconds=3` se aplica a herramientas del registro; las 13 operaciones fiscales usan 35 s y navegador 30 s. Los cuatro auxiliares de consulta/revisión no declaran timeout propio (`backend/app/tools/registry.py:39`, `backend/app/billing/tools.py:326`, `backend/app/billing/tools.py:43`, `backend/app/billing/tools.py:230`). Inventario completo en §3. |
| L3 | **A MEDIAS** | 35 palabras y 160 caracteres pueden cumplirse simultáneamente; 50 caracteres es un objetivo, no otro máximo. Son instrucciones al modelo; Telegram añade un presupuesto de 100 tokens y un recorte duro configurado a 160 caracteres (`backend/app/agent_service.py:20`, `backend/app/agent_service.py:70`, `backend/app/agent_service.py:216`, `backend/app/config.py:65`). `brief_reply` puede eliminar una cláusula posterior relevante o acabar con puntos suspensivos, sin comprender su importancia (`backend/app/replies.py:5`). **No recorta los botones ni las revisiones humanas:** esas se muestran por controles nativos; una revisión demasiado larga pierde la opción de aprobar (`backend/app/billing/telegram_agent.py:73`, `backend/app/billing/telegram_submission.py:59`, `backend/app/billing/telegram.py:779`). |
| L4 | **A MEDIAS** | El chat conserva `result.to_input_list()`, incluidas herramientas, y no tiene resumen ni recorte automático de ese historial (`backend/app/agent_service.py:190`, `backend/app/agent_service.py:236`, `backend/app/database.py:193`). Ensayo C5: 1.110 bytes por interacción con `billing_workspace`, 14 interacciones guardadas = 15.540 bytes; la número 15 falla con `context_limit`. No es un número universal: el tamaño varía y también existe el tope de 20 turnos (`backend/app/config.py:35`, `backend/app/database.py:172`). Las revisiones públicas sí reinician su contexto entre ejecuciones, y los botones nativos no agregan todos esos turnos al chat SDK (`backend/app/database.py:114`, `backend/app/billing/telegram_agent.py:64`). |
| L5 | **A MEDIAS** | Con perfil existente, el recorrido de referencia C6 consumió **8 actualizaciones normales, 1 ejecución SDK, 2 extracciones y 1 búsqueda oficial**, incluyendo SAT y dos descargas. Límites por defecto: 100/30/10; búsquedas tienen un contador separado con el mismo máximo numérico de 10 (`backend/app/config.py:33`, `backend/app/config.py:45`, `backend/app/billing/portal_discovery.py:57`). La cuota de actualizaciones se comprueba antes de despachar `ls`/`se`/`sv`, y puede bloquear una aprobación preparada; estados/menús tienen otra cuota de 500 (`backend/app/billing/telegram.py:840`). Reproducido con aprobación pendiente y cero peticiones privadas, C7. Las lecturas y búsquedas llaman directamente a `Runner`, fuera del registro de tokens/ejecuciones de `AgentService` (`backend/app/billing/extraction.py:86`, `backend/app/billing/extraction.py:144`). Deduplicación Telegram en payloads cifrados de `billing_records`; extracciones y ejecuciones en tablas distintas sin ese cifrado (`backend/app/billing/telegram.py:861`, `backend/app/billing/store.py:27`, `backend/app/database.py:174`). |
| L6 | **A MEDIAS** | Originales generales/CSF: **24 h, máximo 168**; fotos de ticket: **168 h, máximo 168**; XML/PDF: **30 días, máximo 90** (`backend/app/config.py:40`). `purge()` elimina cualquier registro vencido, incluidos `invoice_xml` e `invoice_pdf`; corre al acceder al servicio y periódicamente (`backend/app/billing/store.py:142`, `backend/app/billing/service.py:67`, `backend/app/main.py:70`). No hay archivo durable de CFDI implementado en este flujo; el documento se guarda con expiración (`backend/app/billing/portal_submission.py:196`, `backend/app/billing/service.py:955`). C2 comprobó que quedan estado «completada» y dos IDs sin archivos descargables. Copias descargadas por el usuario o respaldos externos reales: **NO VERIFICADO**. |
| L7 | **A MEDIAS** | Máximos por defecto: **40 documentos y 10 perfiles**, configurables hasta 100/20 (`backend/app/config.py:43`); memoria: **50 portales durante 30 días** (`backend/app/billing/portal_memory.py:168`). Leer/reutilizar no renueva `expires`; incluso guardar el mismo enlace devuelve el registro anterior (`backend/app/billing/portal_memory.py:107`, `backend/app/billing/portal_memory.py:163`). El usuario recibe el límite de archivos y próximo vencimiento, «Alcanzaste el límite de perfiles fiscales» o la instrucción de olvidar un portal (`backend/app/billing/telegram_requests.py:156`, `backend/app/billing/service.py:305`, `backend/app/billing/portal_memory.py:169`). Los XML/PDF cuentan como documentos y la capacidad puede comprobarse después de solicitar la factura: se informa «La factura se solicitó; falta espacio...» (`backend/app/billing/portal_submission.py:193`). |
| L8 | **A MEDIAS** | `AgentService.lock` es global por instancia: otra ejecución SDK recibe `server_busy`, no se encola (`backend/app/agent_service.py:146`, `backend/app/agent_service.py:179`). Para una foto y un botón simultáneos del mismo usuario Telegram opera primero un lock por usuario; el segundo recibe «vuelve a enviar este mensaje» y retorna `busy` antes de registrar la actualización (`backend/app/billing/telegram.py:830`). La extracción nativa tiene otro `analysis_lock`; no todas las fotos pasan por el lock del SDK (`backend/app/billing/service.py:197`). Protege la concurrencia, pero obliga a reintentar al usuario; no demuestra pérdida de datos ya guardados. |
| L9 | **A MEDIAS** | Cliente OpenAI y configuración del SDK tienen `max_retries=0`; extracción y búsqueda lo repiten (`backend/app/agent_service.py:150`, `backend/app/agent_service.py:154`, `backend/app/billing/extraction.py:88`, `backend/app/billing/extraction.py:145`). Un 429/5xx transitorio termina el intento; no hay backoff automático en estos caminos (`backend/app/billing/extraction.py:155`, `backend/app/agent_service.py:240`). El fallo de extracción cuenta y el de chat puede retener toda la reserva (`backend/app/billing/service.py:225`, `backend/app/database.py:174`). Un botón de búsqueda sí crea otro intento explícito; refrescar una sesión obsoleta no es reintentar un envío (`backend/app/billing/portal_discovery.py:63`, `backend/app/billing/tools.py:173`). Correo/envío incierto no se repiten automáticamente, lo cual protege frente a duplicados (`backend/app/billing/providers.py:72`, `backend/app/billing/service.py:812`). |
| S1 | **A MEDIAS** | Se repiten reglas de aprobaciones, fuentes no confiables y referencias en los tres bloques; navegador se adjunta incluso al chat general (`backend/app/agent_service.py:20`, `backend/app/agent_service.py:36`, `backend/app/agent_service.py:64`, `backend/app/agent_service.py:100`). «No rellena/emite» está limitado al **navegador público**: no contradice el envío privado nativo. Sí está desactualizada la exigencia literal de un botón `Permitir` para cada apertura: el flujo v3 concede la ruta con el consentimiento inicial de análisis/búsqueda (`backend/app/agent_service.py:94`, `backend/app/billing/telegram_discovery.py:9`, `backend/app/billing/telegram_discovery.py:26`). |
| S2 | **A MEDIAS** | Sin herramientas extra: **2** base; con facturación, **19**; con navegador habilitado, **21** en chat general. Revisión pública: solo **2**; extracción: **0**; descubrimiento oficial: **1** `WebSearchTool` (`backend/app/tools/registry.py:16`, `backend/app/main.py:67`, `backend/app/billing/tools.py:328`, `backend/app/agent_service.py:185`, `backend/app/billing/extraction.py:86`, `backend/app/billing/extraction.py:144`). Ya existe filtrado por propósito; el chat general podría ocultar más operaciones según etapa/configuración. Los botones nativos no exponen necesariamente esas 21 herramientas en cada actualización. |
| S3 | **A MEDIAS** | Hay historial SDK, transcripción `messages`, estado del canal, ticket, transacción y revisión de envío; guardan tanto referencias como copias de valores/estado (`backend/app/database.py:182`, `backend/app/billing/telegram.py:862`, `backend/app/billing/portal_submission.py:54`, `backend/app/billing/portal_submission.py:81`). No son una sola transacción global. Hay comprobaciones de propietario, alcance y huella antes de enviar; cambios e incertidumbre bloquean reenvíos (`backend/app/billing/portal_submission.py:111`, `tests/test_portal_submission.py:305`). Divergencia demostrada: vencen archivos, pero quedan sus referencias y estado de completada en el ticket, C2 (`backend/app/billing/store.py:142`). Otras corrupciones por carrera: **NO VERIFICADO**. |
| S5 | **NO CUMPLE** | README/NAVEGACIÓN aún describen que no se envía o que el envío/XML/PDF necesitan implementación, aunque existe el circuito privado (`README.md:18`, `docs/NAVEGACION.md:28`, `docs/FACTURACION.md:120`, `backend/app/billing/portal_submission.py:104`). FACTURACIÓN dice 24 h para tickets, frente a 168 h actuales, y «una llamada por documento» no describe las lecturas dirigidas (`docs/FACTURACION.md:177`, `docs/FACTURACION.md:183`, `backend/app/config.py:41`). Los conteos 58/105 están presentados como históricos, no como el conteo actual: no son por sí mismos un error (`docs/VALIDACION.md:134`). Railway opcional, Hostinger elegido y CyberPanel como proxy no se contradicen necesariamente (`README.md:45`, `docs/HOSTINGER.md:1`). |
| K1 | **NO VERIFICADO** operacionalmente | La documentación exige respaldo consistente de SQLite y copia separada de la clave, con prueba de restauración; no demuestra que se ejecuten (`docs/HOSTINGER.md:292`). Se genera una clave al configurar y el actualizador de OpenAI crea una copia local de la configuración antes de cambiarla (`scripts/configure_hostinger.py:37`, `scripts/update_openai_key.py:51`). No se identificó una tarea automática de respaldo/restauración de SQLite en el arranque; las tareas periódicas visibles son purga/seguimiento (`backend/app/main.py:70`). Respaldo externo, copia de clave vigente y restauración exitosa: **NO VERIFICADO**. |
| K2 | **A MEDIAS** | Usa AES-GCM con nonce aleatorio y AAD de tipo/propietario/ID, pero la **misma clave de 32 bytes** alimenta AES y HMAC de `digest()` (`backend/app/billing/store.py:17`, `backend/app/billing/store.py:39`). La envoltura almacena nonce+cifrado y el esquema no incluye `key_id`; no se encontró migración de rotación en ese flujo (`backend/app/billing/store.py:27`, `backend/app/billing/store.py:42`). Cambiar la clave sin migrar rompe descifrado y búsquedas/identidad Telegram basada en HMAC (`backend/app/billing/telegram.py:826`). Esto es ausencia de separación/gestión de claves, no demostración de que AES-GCM esté vulnerado. |
| K3 | **A MEDIAS** | Un RFC enviado como texto ordinario, fuera de un control fiscal nativo, se incorpora al input del modelo y a `messages/history` de SQLite sin el cifrado por registro de `BillingStore` (`backend/app/billing/telegram.py:1291`, `backend/app/agent_service.py:190`, `backend/app/database.py:186`, `backend/app/database.py:193`). C5 lo comprobó con un RFC ficticio. Las instrucciones prohíben copiar datos fiscales en la respuesta, pero no eliminan el RFC del input o del historial (`backend/app/agent_service.py:28`). Los controles fiscales nativos y el aislamiento por propietario sí existen; no se demuestra exposición entre usuarios (`backend/app/billing/store.py:42`, `backend/app/database.py:137`). |
| K4 | **NO CUMPLE** | Hay fecha de compra, pero no fecha límite de facturación por ticket ni cálculo de la ventana del comercio (`backend/app/billing/models.py:92`, `backend/app/billing/models.py:134`). Los plazos implementados aquí son retención del archivo, vigencia de aprobación y tiempo de recorrido, no vencimiento fiscal/comercial (`backend/app/billing/service.py:139`, `backend/app/billing/portal_submission.py:100`, `backend/app/billing/tools.py:159`). La aceptación de un ticket vencido por cada comercio real es **NO VERIFICADO**. |

## 3. Límites superpuestos del navegador (L2)

Valores por defecto del código, no configuración confirmada del VPS. `MiB` significa 1.048.576 bytes.

| Capa | Definición y aplicación | Consecuencia |
| --- | --- | --- |
| Ejecución del agente | 3 turnos del modelo, 5 llamadas de herramienta, 35 s totales (`backend/app/config.py:29`, `backend/app/agent_service.py:134`, `backend/app/agent_service.py:198`, `backend/app/agent_service.py:219`). | Los pasos internos de una herramienta no son turnos SDK adicionales. |
| Herramientas | Registro general: 3 s; 13 operaciones de facturación: `run_timeout_seconds`, 35 s; observar/navegar: 30 s. Los cuatro auxiliares (`billing_workspace` y tres revisiones) no declaran timeout propio (`backend/app/tools/registry.py:39`, `backend/app/billing/tools.py:326`, `backend/app/billing/tools.py:179`, `backend/app/billing/tools.py:43`, `backend/app/billing/tools.py:57`, `backend/app/billing/tools.py:66`, `backend/app/billing/tools.py:230`). | Cambiar `tool_timeout_seconds` no cambia estos otros grupos; los auxiliares siguen bajo el plazo total del Runner. |
| Ruta pública | Plazo de 22 s creado antes de abrir; hasta 7 navegaciones; permisos antiguos por dominio: 1; ciclo por página/etiqueta/tipo (`backend/app/billing/tools.py:159`, `backend/app/billing/tools.py:166`, `backend/app/billing/tools.py:183`, `backend/app/billing/tools.py:207`). | Se comprueba el plazo antes de cada acción; una acción iniciada puede sobrepasarlo. |
| Sesiones worker | TTL público 180 s desde creación, máximo 2 sesiones, máximo 12 orígenes, apertura serializada; 8 pasos por sesión con apertura como primer paso (`backend/browser_worker/engine.py:114`, `backend/browser_worker/engine.py:137`, `backend/browser_worker/engine.py:161`, `backend/browser_worker/engine.py:401`, `backend/browser_worker/engine.py:410`, `backend/browser_worker/engine.py:609`). | Abrir + siete acciones cabe exactamente en ocho pasos, sin margen para otros pasos consumidos. La aprobación de 900 s puede sobrevivir a la sesión de 180 s. |
| Chromium | Apertura: bloque de 10 s y `goto` de 8 s; observación: 6 s, espera de carga de 1 s, espera de selector de marca de 1,2 s; `goto` posterior: 8 s; expansión: 4 s (`backend/browser_worker/engine.py:418`, `backend/browser_worker/engine.py:447`, `backend/browser_worker/engine.py:482`, `backend/browser_worker/engine.py:521`, `backend/browser_worker/engine.py:626`). | Los peores tiempos de ocho pasos no caben en 22/30/35 s. Páginas rápidas sí pueden completar el recorrido. |
| HTTP interno público | Timeout 15 s por llamada y respuesta máxima 65.536 bytes (`backend/app/billing/browser.py:130`, `backend/app/billing/browser.py:149`). | Puede interrumpir una operación del worker antes del límite del SDK. |
| Red del comercio | 60 peticiones/página, 240/ruta; 4 MiB/respuesta, 8 MiB/página, 24 MiB/ruta; modo antiguo 6 MiB; fetch total 8 s, cliente 5 s y máximo 12 conexiones (`backend/browser_worker/policy.py:15`, `backend/browser_worker/policy.py:131`, `backend/browser_worker/policy.py:145`; aplicación de presupuestos en `backend/browser_worker/engine.py:336`). | Límites independientes; recursos pesados pueden agotarlos antes de encontrar el formulario. Algunos presupuestos por página son reanudables, no ilimitados. |
| Representación de página | Hasta 64 frames inspeccionados, 8 frames elegibles, 24 campos/48 acciones; snapshot recortado a 60.000 bytes; exploración acotada de DOM/shadow DOM (`backend/browser_worker/engine.py:489`, `backend/browser_worker/engine.py:572`, `backend/browser_worker/engine.py:585`, `backend/browser_worker/engine.py:24`). Caché auxiliar: 8 MiB/180 s (`backend/browser_worker/resources.py:16`). | La vista puede marcarse parcial y requerir intervención; no prueba ausencia del formulario en la web real. |
| Vista entregada al modelo | Hasta 12 campos, 16 acciones y aproximadamente 3.500 bytes; retira elementos y marca `partial` (`backend/app/billing/tools.py:128`). Contexto total SDK: 16.000 bytes (`backend/app/agent_service.py:122`). | No se debe confundir el snapshot completo de 60.000 bytes con el input que recibe el modelo. |
| Inspector HTML alternativo | Ruta sin Playwright: allowlist, plazo configurable 7 s (máximo 15), cliente HTTP 5 s, HTML máximo 262.144 bytes y una redirección comprobada (`backend/app/config.py:51`, `backend/app/billing/portal_inspection.py:133`, `backend/app/billing/portal_inspection.py:143`, `backend/app/billing/portal_inspection.py:151`, `backend/app/billing/portal_inspection.py:154`, `backend/app/billing/portal_inspection.py:166`). | Son límites de otro camino de inspección; su lista de URLs no es una restricción universal a los portales navegados con Playwright. |
| Protocolo worker | Cuerpo máximo 16.384 bytes; hasta 12 orígenes públicos, 24 valores y 4 orígenes privados (`backend/browser_worker/app.py:22`, `backend/browser_worker/app.py:53`, `backend/browser_worker/app.py:60`, `backend/browser_worker/app.py:87`). | Es otro límite independiente de la página y del historial. |
| Llenado sin enviar | Bloque de 10 s, hasta 24 valores y operaciones de control de 1,2 s (`backend/browser_worker/filling.py:22`, `backend/browser_worker/filling.py:61`, `backend/browser_worker/filling.py:73`). | Esta fase no equivale a envío ni al recorrido público. |
| Envío privado nativo | Hasta 8 operaciones/transacción, 4 orígenes por paso, 24 valores de hasta 250 caracteres; 18 s por paso, llenado de 1,2 s/campo, clic de 6 s y espera de red de 2,5 s (`backend/browser_worker/submission.py:163`, `backend/browser_worker/submission.py:170`, `backend/browser_worker/submission.py:201`, `backend/browser_worker/submission.py:218`). Hasta 6 archivos y presupuesto privado de 24 MiB (`backend/browser_worker/submission.py:130`, `backend/browser_worker/submission.py:143`). | Cada envío requiere su revisión; no hereda automáticamente los 35 s del Runner porque lo ejecuta el manejador nativo. |
| Cliente privado/aprobación | HTTP 25 s; respuesta de 65.536 bytes o 5.600.000 para archivo; aprobación 900 s (`backend/app/billing/browser.py:250`, `backend/app/billing/portal_submission.py:100`). | Vencimiento de aprobación y sesión son relojes distintos. |

**Respuesta a «¿cabe observar + siete pasos?»** Sí en los conteos cuando el avance es automático dentro de una sola herramienta y la sesión empieza nueva. No hay garantía temporal ni compatibilidad universal: 22 s de ruta, recursos, red y complejidad DOM pueden detenerla. Una marca concreta no elimina estos límites (`backend/app/billing/tools.py:161`, `backend/browser_worker/engine.py:609`).

## 4. Caracterizaciones temporales

Se ejecutaron con bases temporales y datos ficticios, mediante Python por stdin; no se guardó ninguna prueba nueva. Los resultados de esta sección son registros de esos ensayos, sustentados por las rutas citadas, no observaciones de producción.

### C1. Calidad de imagen antes de IA (F1)

Entrada: PNG blanca, 1×1 píxel. `inspect_document` devolvió `image/jpeg`, conservando dimensiones `[1, 1]`. Demuestra que formato válido no equivale a legibilidad. La función no llama a IA; la lectura posterior sí se hace en `_extract` (`backend/app/billing/documents.py:29`, `backend/app/billing/service.py:201`, `backend/app/billing/extraction.py:144`).

### C2. CFDI vencido y estado de solicitud (L6/S3)

Se completó el circuito Telegram→portal simulado→XML/PDF→SAT simulado, utilizando Chromium real. Luego se adelantó exclusivamente la caducidad de los dos documentos de esa base temporal y se llamó a `purge()`:

```text
status_before = FACTURACION_COMPLETADA
status_after = FACTURACION_COMPLETADA
reference_count_after = 2
download_errors = [billing_not_found, billing_not_found]
```

La purga borra documentos y no reconcilia las referencias del ticket (`backend/app/billing/store.py:142`). El código de descarga consulta el documento vigente, no restaura el binario desde la validación (`backend/app/billing/api.py:73`, `backend/app/billing/service.py:94`). Esto no demuestra que el usuario carezca de una copia externa.

### C3. Foto desconocida mientras faltan campos (F7)

Con una solicitud activa y total pendiente, se envió una imagen distinta, desconocida, sin caption. Antes de analizarla, el resultado fue:

```text
same_request = true
source_changed = true
request_count = 1
mensaje = Foto actualizada; misma solicitud.
```

Es una ambigüedad de intención resuelta por la heurística `partial`, no por una lectura que demuestre que ambas fotos corresponden a la misma compra (`backend/app/billing/telegram_receipts.py:54`). El caption explícito «otro ticket nuevo» sí tiene prioridad y crea otra solicitud (`tests/test_receipt_conversation_recovery.py:99`). La validación posterior puede rechazar la nueva foto por identidad distinta; eso evita mezclar valores, pero no ejecuta el flujo de nueva solicitud (`backend/app/billing/portal_preparation.py:251`).

### C4. Reserva y fallos de ejecución (L1)

Cálculo con `Settings` por defecto, sin modificar configuración (`backend/app/config.py:99`):

```text
(16000 + 8000 + 150 × 8 × 3) × 3 + 150 × 3 = 83250
max_daily_tokens = 250000
```

Se usaron `AgentService` y el SDK real con un modelo simulado: devolvió una llamada a calculadora con 40 tokens de entrada y 20 de salida; después produjo un error transitorio simulado. El hook había acumulado 60, pero `finish_run(failed=True)` no recibió `actual` (`backend/app/agent_service.py:127`, `backend/app/agent_service.py:256`).

| Intento | Resultado | Consumo conocido de ese intento | Reserva que conserva SQLite | Acumulado contabilizado |
| --- | --- | ---: | ---: | ---: |
| 1 | `agent_error` | 60 | 83.250 | 83.250 |
| 2 | `agent_error` | 60 | 83.250 | 166.500 |
| 3 | `agent_error` | 60 | 83.250 | 249.750 |
| 4 | `daily_limit`, antes del modelo | 0 | No crea cuarta fila | 249.750 |

Las tres filas quedaron con `actual=NULL`, `reserved=83250`, `status=failed`. Quedan 250 tokens contables, insuficientes para reservar otros 83.250 (`backend/app/database.py:174`). Es retención conservadora intencional: no se debe presentar ese acumulado como consumo facturado por OpenAI ni asumir que siempre se conoce todo el coste de un fallo. También es distinto del límite de 10 lecturas de documentos.

### C5. Tamaño de historial y RFC escrito en chat (L4/K3)

Modelo simulado con una llamada a `billing_workspace` y respuesta breve en cada interacción. Se envió un texto ordinario con RFC ficticio `AAA010101AAA`, fuera del formulario fiscal. Con límites por defecto:

```text
bytes por interacción guardada = 1110
interacciones guardadas = 14
historial tras interacción 14 = 15540 bytes
interacción 15 = context_limit
RFC en messages = true
RFC en history = true
RFC en input del modelo = true
```

No puede extrapolarse «14 tickets»: es un ejemplo de chat SDK con herramienta, no de 14 flujos nativos completos. Un resultado de herramienta más grande puede agotar el límite antes; una conversación más breve puede llegar antes al máximo de 20 turnos (`backend/app/agent_service.py:122`, `backend/app/database.py:172`). No existe resumen automático de ese historial; la revisión pública tiene su propio reinicio (`backend/app/database.py:114`).

### C6. Coste de un recorrido representativo (L5)

Se usó el flujo Telegram v3 con búsqueda oficial simulada, navegador Chromium real y un formulario compatible que emite XML/PDF en un único envío. Se ejecutaron después autorización SAT y descarga de ambos archivos. No se emitió una factura real.

| Hito | Actualizaciones normales acumuladas | Extracciones acumuladas | Búsquedas oficiales acumuladas | Ejecuciones AgentService acumuladas |
| --- | ---: | ---: | ---: | ---: |
| Alta y confirmación del perfil | 5 | 1 | 0 | 0 |
| Subir y autorizar ticket; llegar al formulario/datos | 7 | 3 | 1 | 1 |
| Revisar envío privado (`lv`) | 8 | 3 | 1 | 1 |
| Aprobar envío y recuperar XML/PDF (`ls`) | 9 | 3 | 1 | 1 |
| Revisar/autorizar SAT (`sa`, `sv`) | 11 | 3 | 1 | 1 |
| Descargar XML/PDF (`dl`, dos veces) | 13 | 3 | 1 | 1 |

**Por ticket con perfil existente:** restando la primera fila, 8 actualizaciones normales, 2 lecturas, 1 búsqueda y 1 ejecución SDK. La ejecución simulada registró 60 tokens en `runs`; las lecturas y la búsqueda no se sumaron a ese contador porque utilizan `Runner` directamente (`backend/app/billing/extraction.py:90`, `backend/app/billing/extraction.py:150`). El transporte privado tuvo un POST y dos GET de archivos; SAT una consulta; correo ningún envío.

Con 10 extracciones diarias, si cada ticket necesitase esas dos lecturas, cabrían cinco con perfil ya existente; el día del alta, cuatro completos tras consumir una lectura de CSF. Es un cálculo del ejemplo, no una tarifa universal: datos ya disponibles pueden evitar la segunda lectura; fotos, campos adicionales, postbacks, reintentos y correcciones incrementan acciones o lecturas (`backend/app/billing/portal_preparation.py:205`, `backend/app/billing/portal_preparation.py:231`). Las búsquedas tienen un contador distinto aunque compartan el valor máximo 10 (`backend/app/billing/portal_discovery.py:57`).

Una aprobación nativa ya preparada no vuelve a reservar por sí misma una ejecución SDK ni una extracción, pero sigue pasando por el límite normal de actualizaciones Telegram. Los callbacks `ls`, `se` y `sv` no pertenecen a la lista de controles exentos de esa cuota (`backend/app/billing/telegram.py:844`). Por tanto, 100 actualizaciones pueden impedir enviar, aunque los datos y la aprobación existan. `/estado` y los menús conservan su presupuesto separado de 500, y no resuelven por sí mismos el bloqueo del envío (`backend/app/billing/telegram.py:850`).

### C7. Aprobación preparada al agotarse la cuota Telegram (L5)

En otra base temporal se preparó una revisión privada válida y después se completó exclusivamente el contador local de actualizaciones normales hasta 100. Al pulsar su botón `ls`:

```text
response = {ok: true, rate_limited: true}
review_state = pending
private_requests = 0
mail_sends = 0
```

El servidor no consumió la aprobación ni envió datos: la cuota impidió llegar al callback. Demuestra el bloqueo de una aprobación ya preparada, no una emisión duplicada ni un envío sin autorización (`backend/app/billing/telegram.py:850`, `backend/app/billing/telegram.py:867`).

## 5. Camino efectivo de búsqueda y extracción (F2/F3)

En Telegram, la recepción ofrece el alcance `portal-fields-v3` porque el extractor de producción declara `routing_version=3`; el callback autorizado registra ese alcance y llama a `analyze_ticket` (`backend/app/billing/extraction.py:58`, `backend/app/billing/telegram.py:250`, `backend/app/billing/telegram.py:1017`). Primero se ejecuta `extract_routing`, que lee comercio y medios de facturación, pero también datos básicos de compra. No es una lectura exclusivamente de URL, ni todavía una extracción dirigida por el formulario (`backend/app/billing/extraction.py:66`).

Después se descarta una URL identificada como encuesta/promoción y se considera el QR local bajo condiciones de evidencia. Un contacto oficial configurado puede aportar correo. Se intenta seleccionar un portal recordado, cuya clave depende de marca, RFC emisor y origen de una URL ya existente (`backend/app/billing/service.py:327`, `backend/app/billing/service.py:357`, `backend/app/billing/service.py:374`, `backend/app/billing/portal_memory.py:92`). Esa dependencia impide recuperar directamente la memoria solo con comercio/RFC cuando la nueva foto no trae URL.

Si falta URL en v3, `PortalDiscovery.find` se ejecuta, pero vuelve inmediatamente si hay correo explícito. Si no, considera la web impresa del comercio, una búsqueda oficial cacheada y finalmente una nueva búsqueda con `WebSearchTool`; comprueba que hubo búsqueda y fuentes, no solo una URL generada por el modelo (`backend/app/billing/service.py:376`, `backend/app/billing/portal_discovery.py:43`, `backend/app/billing/portal_discovery.py:46`, `backend/app/billing/portal_discovery.py:50`, `backend/app/billing/extraction.py:86`, `backend/app/billing/extraction.py:93`). Con URL y consentimiento de ruta, se abre el navegador y se siguen controles públicos hasta el formulario o un límite/intervención (`backend/app/billing/telegram_discovery.py:15`, `backend/app/billing/tools.py:153`). Por tanto, sí hay búsqueda implementada de la sección de facturación de la web oficial, no una simple instrucción al usuario; su éxito en cada web real sigue **NO VERIFICADO**.

El selector de canal todavía prioriza adaptadores autorizados configurados, luego URL, después correo explícito y finalmente manual; su etiqueta `portal_manual` y mensaje «No hay automatización instalada» no describen bien el envío privado posterior (`backend/app/billing/service.py:644`). La lectura dirigida se hace cuando `public_form_ready` ya es verdadero; pide campos del portal más identidad básica, y puede omitirse si los valores ya están disponibles o confirmados por el usuario (`backend/app/billing/portal_preparation.py:192`, `backend/app/billing/portal_preparation.py:205`, `backend/app/billing/extraction.py:125`).

La API web `/documents/{id}/analyze` no selecciona por sí misma el alcance v3; sigue disponible la ruta genérica `extract` (`backend/app/billing/api.py:86`, `backend/app/billing/service.py:212`). Esta coexistencia es comprobable en código. Cuál versión/configuración está corriendo actualmente en el VPS es **NO VERIFICADO**.

## 6. Envío real: alcance de lo que sí está conectado (F6)

El circuito privado no es solo un prompt ni una copia local del formulario:

1. Telegram muestra valores y procedencia, prepara aprobación por paso y la elimina de una revisión demasiado larga (`backend/app/billing/telegram_submission.py:12`, `backend/app/billing/telegram_submission.py:59`).
2. `PortalSubmission.send` verifica vigencia, propietario, perfil, fuente, observación y huella; reclama la aprobación antes del efecto y consulta estado si pierde una respuesta (`backend/app/billing/portal_submission.py:104`).
3. `PortalBrowserClient` autentica la petición interna; las rutas `/transaction` llaman a `BrowserManager`, que delega al módulo de envío (`backend/app/billing/browser.py:242`, `backend/browser_worker/app.py:129`, `backend/browser_worker/engine.py:673`).
4. El worker valida referencias/controles/destinos, marca incertidumbre antes del clic, rellena y ejecuta el paso. No vuelve a enviar una operación ya emitida o incierta (`backend/browser_worker/submission.py:147`, `backend/browser_worker/submission.py:194`).
5. Captura XML/PDF observados y permite recuperarlos por referencia/hash; `PortalSubmission` valida y guarda los documentos cifrados con caducidad (`backend/browser_worker/submission.py:125`, `backend/app/billing/portal_submission.py:166`).
6. La validación del XML compara RFC receptor, CP, régimen, uso CFDI, nombre, importe, UUID y otros atributos; `sat_verified` comienza falso. No se hace verificación criptográfica local del sello (`backend/app/billing/cfdi.py:16`, `backend/app/billing/cfdi.py:49`). SAT solo se consulta con autorización y habilitación, y la terminación completa exige documentos y validación (`backend/app/billing/service.py:975`, `backend/app/billing/service.py:981`).

La prueba `tests/test_portal_submission.py:51` ejecuta dos formularios distintos y dos aprobaciones nativas. `tests/test_portal_submission.py:184` comprueba el recorrido Telegram completo, archivos, validación y SAT simulado. Las de XML incorrecto, cambio de destino, propietario ajeno, sesión, respuesta perdida y sustitución de acción cubren fallos concretos, no cualquier HTML imaginable (`tests/test_portal_submission.py:93`, `:214`, `:248`, `:294`, `:305`, `:445`).

**NO VERIFICADO:** interacción con un comercio real, entrega efectiva en el buzón final y aceptación de todos los tipos de formularios. CAPTCHA, login, controles no soportados y snapshots parciales están diseñados para detener el envío, no para superarlos automáticamente (`backend/browser_worker/submission.py:157`).

## 7. Cinco hallazgos más graves, ordenados

Orden por impacto y evidencia disponible, sin atribuir incidentes de producción no demostrados.

1. **Persistencia fiscal desigual entre controles y chat.** Un RFC escrito como texto ordinario llega al modelo y permanece en `messages/history` sin el cifrado por registro fiscal. Reproducido con un dato ficticio en C5; no se demuestra acceso entre usuarios. Evidencia: `backend/app/billing/telegram.py:1291`, `backend/app/agent_service.py:190`, `backend/app/database.py:186`, `backend/app/database.py:193`. Prioridad propuesta: definir explícitamente el tratamiento de datos fiscales en texto libre y su retención; no basta con pedir al modelo que no los repita.
2. **Conservación temporal de CFDI y estado que sobrevive a sus archivos.** XML/PDF vencen a los 30 días por defecto (máximo 90), se borran y el ticket puede seguir «completado» con IDs no descargables. Además, la falta de espacio puede descubrirse después de solicitar la factura. Reproducido C2; respaldo externo real **NO VERIFICADO**. Evidencia: `backend/app/config.py:42`, `backend/app/billing/store.py:142`, `backend/app/billing/portal_submission.py:193`. Prioridad propuesta: decidir archivo/exportación durable y presentar fielmente la disponibilidad de documentos; comprobar capacidad antes del efecto externo.
3. **Las cuotas pueden inmovilizar una solicitud sin corresponder al coste conocido o al trabajo pendiente.** Cada fallo SDK conserva 83.250 de reserva; después de tres, el cuarto se rechaza con 249.750 contabilizados, aunque el consumo conocido haya sido pequeño. Es política conservadora, no gasto real demostrado; reproducido C4. Además, 100 actualizaciones Telegram bloquean una aprobación de envío ya preparada sin consumirla, C7. Evidencia: `backend/app/config.py:99`, `backend/app/agent_service.py:255`, `backend/app/database.py:174`, `backend/app/billing/telegram.py:850`. Prioridad propuesta: distinguir consumo conocido, consumo incierto y reserva de admisión, y decidir cómo tratar las aprobaciones pendientes al agotar mensajes; no rebajar a ciegas una estimación de costes desconocidos.
4. **La intención de nueva foto y nuevo ticket sigue siendo ambigua.** Si faltan campos, una imagen sin texto puede reemplazar la fuente del activo; no abre la otra solicitud ni pausa la anterior. Reproducido C3. La protección de identidad posterior evita combinar valores incompatibles, pero no corrige por sí sola la intención. Evidencia: `backend/app/billing/telegram_receipts.py:54`, `backend/app/billing/portal_preparation.py:251`. Prioridad propuesta: resolver la ambigüedad conservando ambos archivos y la solicitud, sin cancelar silenciosamente.
5. **La cadena de localización de facturación no respeta el orden solicitado.** La memoria guardada exige URL de entrada; cuando no está, no sirve como recurso por comercio. Un correo explícito evita buscar primero un portal oficial. La búsqueda oficial y Playwright sí existen: el problema es la selección de caminos, no falta total de herramientas. Evidencia: `backend/app/billing/portal_memory.py:92`, `backend/app/billing/portal_discovery.py:43`, `backend/app/billing/extraction.py:86`. Prioridad propuesta: definir un único orden verificable de resolución y mantener la distinción entre encuesta, web oficial, portal y correo.

Estas prioridades son recomendaciones de auditoría. No se implementó ninguna corrección en este encargo.

## Estado de correcciones

2026-10-30. Encargo posterior de corrección, en `mejoras-flujo`, partiendo de `16a35e3`. La auditoría anterior conserva sus filas y evidencias históricas sin modificaciones. Se corrigieron exclusivamente L1, L5 y F7; no se desplegó el servicio.

### Commits y pruebas que los cubren

| Punto | Estado | Commit de prueba, confirmado en rojo antes de corregir | Commit de corrección | Pruebas principales |
| --- | --- | --- | --- | --- |
| L1 | **CORREGIDO** | `18089c123e8b989626d290b48e5363505715fc15` | `132f857647a6aae6a5fe8faceca4c6bc327cb4b4` | `tests/test_failed_run_accounting.py::test_three_partially_measured_failures_allow_a_fourth_run`; `test_large_successful_usage_is_not_capped_and_still_blocks_admission`; `test_unmeasured_failure_uses_utf8_input_estimate_and_capped_floor`; `test_restart_accounts_unmeasured_runs_at_floor_and_preserves_actual_usage`; `test_limits_show_the_accounted_failed_run_cost`; `test_failed_run_floor_setting_has_default_and_bounds`. |
| L5 | **CORREGIDO** | `2cc1f428cfc6e300de253d9982c13a33b86e81d3` | `186bbd2cb94caca58f15ebceced5e129ace61b8f` | `tests/test_prepared_approval_quota.py::test_prepared_portal_approval_works_at_message_limit_and_never_sends_twice`; `test_pending_email_can_be_approved_or_rejected_at_message_limit`; `test_sat_confirmation_for_a_validated_invoice_works_at_message_limit`; `test_sdk_pending_approval_or_rejection_uses_control_quota`; `test_prepared_fiscal_review_can_be_confirmed_or_discarded_at_message_limit`; `test_only_an_owned_pending_review_can_use_control_quota`; `test_prepared_approval_still_respects_the_separate_500_control_limit`. |
| F7 | **CORREGIDO** | `633f25b50a574e69444a243943edf0e6d752148c` | `c31eb0925f1e66c34ce87c121af84c04b1b64e1f` | `tests/test_ambiguous_receipt_photo.py::test_unknown_photo_is_saved_without_replacing_or_reading_and_asks_two_choices`; `test_same_choice_replaces_source_once_even_when_the_photo_consumed_update_100`; `test_other_choice_pauses_previous_and_opens_a_new_request_without_mixing_data`; `test_requested_field_photo_keeps_its_recorded_expectation_and_applies_without_a_ticket_choice`; `test_expired_foreign_or_superseded_decision_never_changes_the_active_source`; `test_pending_photo_counts_once_at_document_limit_and_decisions_reuse_it`; `test_identical_bytes_keep_the_same_ticket_without_an_ambiguity_prompt_or_new_read`. |

### Ejecuciones de verificación

Dependencias comprobadas con `uv sync --frozen --group browser`. Suites completas ejecutadas con `uv run --frozen --group browser pytest -q -r a`, guardando XML JUnit y logs temporales fuera del repositorio:

| Etapa | Aprobadas | Fallidas/errores/omitidas | Tiempo de pytest |
| --- | ---: | --- | --- |
| Línea base del nuevo encargo, `16a35e3` | 798 | 0 / 0 / 0 | 416,32 s |
| Después de L1, `132f857` | 815 | 0 / 0 / 0 | 432,04 s |
| Después de L5, `186bbd2` | 827 | 0 / 0 / 0 | 435,91 s |
| Después de F7, suite final, `c31eb09` | **838** | **0 / 0 / 0** | **447,93 s** |

Antes de cada corrección se ejecutaron las pruebas del problema con la implementación anterior y se guardó el commit de prueba antes del commit de código:

| Punto | Resultado en rojo | Comprobación focalizada después de corregir |
| --- | --- | --- |
| L1 | 17 fallidas y 2 aprobadas en 14,45 s; la cuarta ejecución seguía devolviendo `daily_limit`. Incluye las dos aserciones antiguas autorizadas. | 49 aprobadas en 26,56 s: archivo nuevo más `tests/test_limits.py` y `tests/test_api.py`. |
| L5 | 8 fallidas y 4 aprobadas en 15,28 s; los botones preparados quedaban bloqueados por la cuota normal. | 12 aprobadas en 15,91 s. |
| F7 | 9 fallidas y 5 aprobadas en 9,22 s; la fuente cambiaba antes de pedir intención. Incluye el caso antiguo `[None]`. | 52 aprobadas en 58,48 s: archivo nuevo más recuperación de conversación y workflow. |

Las pruebas positivas de regresión que ya pasaban antes se conservaron. Los 40 casos adicionales explican el aumento de 798 a 838; no se eliminó ninguna prueba ni parámetro existente. Evidencias temporales: `/tmp/lilyapp-fixes-baseline.xml`, `/tmp/lilyapp-l1-suite.xml`, `/tmp/lilyapp-l5-suite.xml`, `/tmp/lilyapp-fixes-final.xml` y logs `/tmp/lilyapp-l1-red.log`, `/tmp/lilyapp-l5-red.log`, `/tmp/lilyapp-f7-red.log`.

### Decisiones de implementación

**L1.** La reserva sigue utilizándose para admitir una ejecución. Se añadió `failed_run_floor_tokens`, default 2000, rango 0–100000. En un fallo se suman tokens conocidos y la estimación UTF-8 de la entrada de la llamada en vuelo (`bytes // 3`), con tope en la reserva. Cuando no hay consumo conocido se aplica como mínimo el piso, también acotado. Un observador de entrada del SDK devuelve intacto el input y permite distinguir una llamada aún sin `usage` de una ya medida, sin modificar `SafetyHooks` (`backend/app/config.py:35`, `backend/app/agent_service.py:200`, `backend/app/agent_service.py:265`).

Al arrancar se aplica el mismo piso a filas `interrupted` con `actual=NULL`, incluidas las que se convierten desde `running`; también se regularizan fallos históricos `failed` sin consumo conocido. Filas con `actual` informado mantienen su valor. Se conserva el bloqueo de conversaciones interrumpidas para no repetir efectos inciertos (`backend/app/database.py:15`, `backend/app/database.py:45`). El consumo real de una ejecución exitosa no se recorta: el ensayo de 250001 tokens lo contabiliza íntegro y rechaza la siguiente admisión. `/limites` consulta ese mismo ledger actualizado (`tests/test_failed_run_accounting.py:47`, `tests/test_failed_run_accounting.py:101`). Este contador no es una conciliación de la factura del proveedor.

**L5.** Los siguientes callbacks resuelven revisiones guardadas y pasan al presupuesto de controles, sujeto al máximo existente de 500. El propietario y la vigencia se comprueban al consultar los registros; los servicios conservan sus comprobaciones y consumos de aprobación. La clasificación está en `backend/app/billing/telegram.py:841`; su aplicación al contador, en `backend/app/billing/telegram.py:881`.

| Prefijo | Evidencia de revisión ya existente y motivo de clasificación |
| --- | --- |
| `ls` | `portal_send_review` pendiente; aprueba un paso privado. Manejador: `backend/app/billing/telegram_submission.py:67`. |
| `se`, `no` | `email_approval` pendiente; enviar o rechazar el correo revisado. Manejadores: `backend/app/billing/telegram.py:1125`, `backend/app/billing/telegram.py:1133`. |
| `ga`, `gr` | Aprobación SDK persistida en la conversación del propietario, con ID coincidente y vigencia. Manejador: `backend/app/billing/telegram.py:1025`; validación existente: `backend/app/billing/telegram_agent.py:98`. |
| `pf`, `pd` | Confirmación de `profile_review` pendiente o descarte de su borrador propio. La creación y consumo de la revisión están en `backend/app/billing/telegram_profiles.py:139`, `backend/app/billing/telegram_profiles.py:155`; descarte: `backend/app/billing/telegram.py:1109`. |
| `xa`, `xg` | `portal_data_review` o `portal_fill_review` pendientes: autorizan lectura dirigida o llenado ya revisados. Manejadores: `backend/app/billing/telegram.py:947`, `backend/app/billing/telegram.py:969`. Sus cuotas de lectura y validaciones propias permanecen. |
| `la`, `lu` | `portal_field_photo` pendiente para leer, o estado `read` con valor para confirmar. `read` representa una confirmación aún pendiente de la persona. Manejadores: `backend/app/billing/telegram_submission.py:86`, `backend/app/billing/telegram_submission.py:90`. |
| `wa` | `portal_permission` pendiente; la decisión sigue en el manejador original. `wx` ya era un control y se conservó sin cambiar la lógica de permisos (`backend/app/billing/telegram.py:880`, `backend/app/billing/telegram.py:1094`). |
| `sv` | No existe un registro separado de aprobación SAT: la revisión se representa mediante la validación XML guardada en el ticket, todavía sin `sat_verified`. Se clasifica solo ese caso. La autorización explícita continúa en el callback y servicio existentes (`backend/app/billing/telegram.py:859`, `backend/app/billing/telegram.py:1142`, `backend/app/billing/service.py:981`). No se añadió una supuesta garantía de `claim` SAT inexistente. |
| `fw` | `workflow_choice` pendiente del propietario y una acción de decisión válida: permite resolver el menú preparado, incluido F7. El consumo continúa en `TelegramWorkflow.choose` (`backend/app/billing/telegram_workflow.py:243`). |

Crear una revisión, comenzar búsquedas o iniciar análisis nuevos no recibe por ello una exención general. Texto libre y fotos nuevas siguen en la cuota normal de 100; la deduplicación por `update_id` sigue antes de efectos y reconoce ambos tipos de registros. El presupuesto separado de 500 sigue aplicándose. Las cuotas de lecturas y la admisión SDK no se han eliminado (`backend/app/billing/telegram.py:830`, `backend/app/billing/telegram.py:875`, `tests/test_prepared_approval_quota.py:40`, `tests/test_prepared_approval_quota.py:141`).

**F7.** Una foto desconocida, sin intención ni expectativa, cuando hay campos pendientes, se guarda sin extraer y ofrece exactamente «Corrige este mismo ticket» / «Es otro ticket». Se reutiliza `workflow_choice`, propietario, vencimiento de 1800 s y consumo de uso único existentes; los callbacks tienen menos de 64 bytes. La opción de mismo ticket usa `replace_source`; la otra pausa y abre una solicitud limpia utilizando el mismo documento guardado (`backend/app/billing/telegram_receipts.py:60`, `backend/app/billing/telegram_workflow.py:157`, `backend/app/billing/telegram_workflow.py:243`). El archivo cuenta una sola vez para el límite, incluso después de elegir cualquiera de las opciones (`tests/test_ambiguous_receipt_photo.py:134`).

No fue necesario añadir expectativas de fotos de campo: ya se registran como `ticket_input` o `portal_field_input` antes de pedirlas, y el manejador de campo se ejecuta antes de la recepción genérica. Se comprobó tanto el dato del ticket como el campo del formulario privado, incluso aprobando lectura y valor después de agotar los 100 mensajes (`backend/app/billing/telegram.py:941`, `backend/app/billing/telegram_submission.py:37`, `backend/app/billing/telegram_submission.py:81`, `backend/app/billing/telegram_submission.py:126`, `tests/test_ambiguous_receipt_photo.py:82`). Las fotos conocidas conservan su deduplicación; la intención explícita de otro ticket y las comprobaciones posteriores de identidad siguen en sus caminos existentes.

### Pruebas existentes modificadas y protección del alcance

- `tests/test_limits.py::test_timeout_releases_conversation_and_accounts_uncertain_usage`: cambió exclusivamente su antigua igualdad con la reserva completa. La nueva única línea comprueba `piso <= tokens_accounted == min(reserva, max(piso, bytes_del_input // 3)) < reserva`, calculando el input `Hola` dentro de la propia prueba. El 504 y la liberación de `busy` quedaron idénticos.
- `tests/test_api.py::test_errors_and_audit_never_echo_credentials_or_prompts`: cambió exclusivamente esa misma igualdad antigua, usando su variable `prompt` para calcular la estimación. Conserva idénticas todas las comprobaciones de no filtrar secretos, contraseña o prompt.
- `tests/test_receipt_conversation_recovery.py::test_a_new_photo_after_missing_data_updates_the_same_request[None]`: se añadieron tres líneas para comprobar que la fuente anterior permanece hasta elegir «Corrige este mismo ticket». Antes codificaba precisamente la sustitución automática que F7 corrige. Se mantuvieron el parámetro `None`, los otros captions y todas las comprobaciones posteriores de propietario, lectura y reutilización del perfil.

Se verificó mediante comparación de código/AST contra `16a35e3`: `SafetyHooks`, `needs_approval`, `store.claim`, AES-GCM/digest, permisos de portales, retención, `token_reservation`, default de `max_daily_tokens`, `Database.reserve_run`, `finish_run` y `usage` permanecen intactos. Los archivos `tests/test_limits.py` y `tests/test_api.py` tienen exactamente una línea sustituida cada uno; el resto coincide con la base. No se modificaron dependencias, Compose ni workflows.

**NO VERIFICADO:** despliegue/configuración del VPS, interacción real con Telegram/OpenAI, OCR de fotos reales, coste efectivamente facturado, emisión ante comercios reales, entrega real por correo y consulta real al SAT. Las pruebas verifican el SDK y, para los pasos del portal, Chromium real con transporte/proveedores simulados. No se realizaron envíos ni despliegues reales.
