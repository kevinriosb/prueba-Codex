# Navegación adaptable: primera fase pública

El mismo agente de OpenAI puede observar controles reales de cada página, seguir
un enlace observado y desplegar alternativas como «No cuento con Web ID».
No hay una secuencia fija por comercio. Los nombres de campos se relacionan con
conceptos de los documentos: Cuenta, Folio, Referencia, Web ID, caseta, carril,
fecha y hora. Un campo desconocido queda para revisión humana.

Esta entrega **no rellena formularios, no busca la compra con sus identificadores,
no envía el perfil fiscal, no inicia sesión y no solicita o emite facturas**.
Registrar tickets, introducir los datos fiscales, aprobar la operación, enviar
el formulario y recuperar sus XML/PDF son la siguiente fase. El envío por Gmail
existente es una integración separada con sus propias aprobaciones.

## Qué necesitas

- El VPS ya preparado, Docker y la aplicación existente.
- Espacio para la imagen de Chromium y hasta 768 MB de RAM para su contenedor.
- Los dominios HTTPS exactos del comercio y del proveedor de facturación, incluidos
  los destinos de sus enlaces. Autorizar dominios no configura una receta de clics.
- La clave de OpenAI existente: las decisiones de navegación usan el mismo agente,
  modelo y presupuestos. No necesitas otra cuenta ni una clave de navegador.

El navegador se ejecuta en otro contenedor, sin `.env.hostinger`, clave de OpenAI,
contraseña de la aplicación, clave de cifrado ni volumen de documentos. El backend
solo le proporciona la dirección del portal, dominios permitidos y un propietario
opaco. El navegador mantiene cookies públicas separadas por usuario y efímeras.
Las observaciones no incluyen valores de inputs, HTML completo o cookies.

## Activar desde Termius

La activación es opcional. El despliegue anterior con `compose.hostinger.yaml`
solo conserva su comportamiento; actualizar GitHub no inicia este servicio.

```bash
(
  set -e
  cd /opt/lilyapp-agent
  git pull --ff-only
  python3 scripts/configure_browser.py
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml build agent browser
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml up -d --wait --wait-timeout 180
  curl --fail --silent --show-error --max-time 20 https://agente.lilyapp.mx/health
)
```

El asistente pide una lista de dominios, por ejemplo
`https://comercio.example,https://proveedor.example` (ejemplos ficticios).
Usa los dominios que hayas comprobado en el ticket y en Safari. Si la página pasa
a otro dominio, debes autorizar también ese destino. El sitio debe aceptar HTTPS
con certificado válido, incluso si el ticket imprime HTTP. No se continúa por HTTP.
No hay comodines ni autorización automática de dominios encontrados en documentos.
Las consultas y fragmentos de la URL inicial no se transmiten en esta fase pública.
Si un portal depende de esos parámetros, puede requerir una adaptación posterior.

El script cambia únicamente `BILLING_BROWSER_ORIGINS` y guarda una copia privada
anterior. Conserva la clave de cifrado, credenciales, límites y base de datos.
Enter sin dominios cancela la secuencia antes de construir o iniciar servicios.
La primera construcción descarga Chromium y puede tardar varios minutos.

El complemento crea una credencial aleatoria **interna**, sin mostrarla, en un
volumen separado. No sustituye la contraseña de usuario ni la clave de cifrado.
El servicio no publica un puerto al exterior. Los dos archivos Compose deben
usarse juntos en las siguientes actualizaciones para conservar esta activación.

## Probar desde Telegram

1. Escribe `/estado` o elige un ticket en `/tickets`.
2. Pulsa **Revisar portal con IA**. Consume una ejecución de chat con sus límites.
3. El agente observa la página y puede ejecutar un paso público por mensaje.
4. Si necesita otro paso, aparece **Continuar revisión con IA**. No se reanuda solo.
5. Revisa los campos descubiertos y sus alternativas. **Cuenta**, **Folio** y
   **Web ID** se mantienen separados; no se completan entre sí por suposición.

Si faltan datos visibles en la foto original, el ticket anterior a esta versión
permite **Autorizar nueva lectura guardada** para extraer caseta, carril, hora o
Web ID. Es otra lectura de IA, explícita y sujeta a la cuota compartida de archivos.
La CSF y el perfil confirmado no se reinician. También se pueden completar campos
obligatorios reconocidos usando los botones **Completar dato** y `/dato_ticket`.

## Límites y comprobaciones

- Se usa un único agente; Playwright es una herramienta, no otro agente.
- Hasta ocho pasos y 180 segundos por sesión pública; el backend conserva la
  revisión 150 segundos. Hasta dos contextos de navegador a la vez.
- Cada ejecución conserva los límites existentes de chat: pasos del SDK, tokens,
  tiempo y cuota diaria. No se elevan automáticamente ni se ocultan llamadas extra.
- Referencias observadas de un solo uso; una página cambiada exige nueva revisión.
- Solo peticiones HTTPS GET a dominios autorizados. IPs privadas, redirecciones no
  autorizadas, métodos POST, WebSockets, descargas y service workers se bloquean.
  Las conexiones HTTP verifican DNS público y fijan IP, Host y SNI para TLS.
- El navegador no recibe secretos fiscales. Su tráfico pasa por el filtro del
  servidor; la salida directa del navegador usa un proxy local que no da servicio.
  El contenedor usa UID 10001, raíz de solo lectura, límites de memoria/procesos y
  ninguna capacidad adicional. No se afirma que Chromium use su sandbox interno.
- Imágenes y medios se omiten en esta fase basada en DOM. CAPTCHA, login, páginas
  solo gráficas, controles incompatibles, recursos no autorizados o ambigüedad
  requieren intervención humana. No hay garantía de compatibilidad universal.

Las pruebas locales usan Chromium real con páginas ficticias y el SDK real con
respuestas de modelo programadas. **No se ha probado la navegación de producción
con los portales reales ni se han realizado llamadas nuevas de OpenAI de pago.**

## API para extender la interfaz web

Con autenticación, origen válido y CSRF:

- `POST /api/billing/tickets/{id}/browser`: abre o recupera la revisión propia.
- `POST /api/billing/tickets/{id}/browser/actions`: recibe exclusivamente
  `observation_id` y `reference` obtenidos de esa revisión.

El chat web puede elegir el ticket con `portal_browser_observe(identifier)` y
seguir una referencia observada. No existe una API para ejecutar JavaScript,
selectores arbitrarios, escribir valores, enviar formularios o saltar aprobaciones.

## Desactivar conservando datos

Desde el directorio de la aplicación:

```bash
docker compose -f compose.hostinger.yaml -f compose.browser.yaml stop browser
docker compose -f compose.hostinger.yaml up -d --no-deps --no-build --force-recreate --wait agent
```

El archivo `.env.hostinger` debe conservar `BILLING_BROWSER_ENABLED=false` o no
definirlo; el complemento es quien lo habilita para el servicio. No ejecutes
`down -v`: no hace falta borrar los volúmenes para activar o desactivar esta función.
