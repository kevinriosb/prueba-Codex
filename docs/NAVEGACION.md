# Navegación adaptable: primera fase pública

El mismo agente de OpenAI puede observar controles reales de cada página, seguir
un enlace observado y desplegar alternativas como «No cuento con Web ID».
No hay una secuencia fija por comercio. Los nombres de campos se relacionan con
conceptos de los documentos: Cuenta, Folio, Referencia, Web ID, caseta, carril,
fecha y hora. Un campo desconocido queda para revisión humana.

Las instrucciones de investigación son las mismas en web y Telegram: observar
la página actual, elegir un control útil y reevaluar el resultado de cada acción.
Un menú, selector de marcas, enlace directo o alternativa son posibilidades, no
una secuencia obligatoria. Un formulario ya visible se revisa directamente.
El bot no pide elegir marca por ver un único logo, aunque se repita en el pie.
No se mantiene una receta por dominio; guardar una liga conserva un punto de
entrada confirmado, no los pasos ni las autorizaciones de otro ticket.

Los botones de revisión usan un contexto público limpio, independiente del
historial y límite de turnos del chat general. Las cuotas diarias se comparten.
Una referencia antigua se actualiza sin repetir el clic; los permisos vencidos
se solicitan de nuevo. Véase [recuperación del portal](RECUPERACION_PORTAL.md).

Esta entrega **no rellena formularios, no busca la compra con sus identificadores,
no envía el perfil fiscal, no inicia sesión y no solicita o emite facturas**.
Registrar tickets, introducir los datos fiscales, aprobar la operación, enviar
el formulario y recuperar sus XML/PDF son la siguiente fase. El envío por Gmail
existente es una integración separada con sus propias aprobaciones.

## Qué necesitas

- El VPS ya preparado, Docker y la aplicación existente.
- Espacio para la imagen de Chromium y hasta 768 MB de RAM para su contenedor.
- El portal indicado en el ticket. Cada usuario lo autoriza desde Telegram; no
  necesitas conocer de antemano todos los comercios ni editar dominios en Termius.
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
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml build agent browser
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml up -d --wait --wait-timeout 180
  curl --fail --silent --show-error --max-time 20 https://agente.lilyapp.mx/health
)
```

No se configura una lista fija de establecimientos. El bot muestra el dominio del
ticket y pide **Permitir / Cancelar** antes de abrirlo. En Telegram este permiso
cubre el recorrido público de facturación: enlaces observados, marca coincidente,
proveedores y recursos funcionales. No se pide otro permiso por cada dominio o
CDN. Los permisos antiguos por dominio requieren esta nueva autorización antes
de ampliarse. La API web conserva el consentimiento por dominio.

El permiso es individual, para ese ticket y su URL de origen exacta, y dura
30 minutos. La confirmación pendiente dura como máximo 15 minutos; para un dominio
observado en una página dura lo que reste de la revisión (150 segundos como máximo).
Los botones son de un solo uso. Cancelar revoca los permisos de esa revisión y
cierra su navegador, conservando perfil y ticket. Escribir «sí» no pulsa el botón.

Permitir autoriza solo lectura y navegación pública con IA; no autoriza escribir
el perfil, registrar compras ni emitir facturas. Esos pasos tendrán una aprobación
separada al implementarse. Cada sitio debe aceptar HTTPS con certificado válido,
aunque el ticket imprima HTTP. No se continúa por HTTP ni hacia redes privadas.
Las consultas y fragmentos de la URL inicial no se transmiten en esta fase pública.
Si un portal depende de esos parámetros, puede requerir una adaptación posterior.
Las redirecciones se presentan como referencias observadas para un paso posterior;
no se dejan a Chromium fuera del filtro de red. En el recorrido autorizado, los
recursos admiten hasta tres redirecciones adicionales, todas con DNS/IP/TLS
validados; las cookies del origen anterior no se reenvían en esos saltos.

Las configuraciones antiguas de `BILLING_BROWSER_ORIGINS` ya no se usan y no
sustituyen el consentimiento. `scripts/configure_browser.py` informa del cambio
sin editar archivos. Las claves, límites, perfil y base de datos se conservan.
La primera construcción descarga Chromium y puede tardar varios minutos.

El complemento crea una credencial aleatoria **interna**, sin mostrarla, en un
volumen separado. No sustituye la contraseña de usuario ni la clave de cifrado.
El servicio no publica un puerto al exterior. Los dos archivos Compose deben
usarse juntos en las siguientes actualizaciones para conservar esta activación.

## Probar desde Telegram

1. Escribe `/estado` o elige un ticket en `/tickets`.
2. Revisa el dominio mostrado y pulsa **Permitir**. Antes de ese botón no se abre
   el portal; consultar `/estado` o cancelar no usa IA.
3. La revisión autorizada usa una ejecución de chat con sus límites. El agente
   observa la página y sigue opciones inequívocas hasta el formulario.
4. La marca coincidente y los recursos no requieren otro botón. Ante ambigüedad,
   CAPTCHA o límite, se muestra el estado. **Continuar revisión con IA** inicia
   otra ejecución dentro de las cuotas existentes.
5. Revisa los campos descubiertos y sus alternativas. **Cuenta**, **Folio** y
   **Web ID** se mantienen separados; no se completan entre sí por suposición.

Si faltan datos visibles en la foto original, el ticket anterior a esta versión
permite **Autorizar nueva lectura guardada** para extraer caseta, carril, hora o
Web ID. Es otra lectura de IA, explícita y sujeta a la cuota compartida de archivos.
La CSF y el perfil confirmado no se reinician. También se pueden completar campos
obligatorios reconocidos usando los botones **Completar dato** y `/dato_ticket`.

## Elegir la marca y recordar su portal

En una página que agrupa varias marcas, el navegador lee el texto, `alt`, `title`
y el nombre público del archivo de los logos. No necesita descargar esas imágenes
ni hacer otra lectura con IA. También reconoce destinos literales observados de
`window.open`, `location.href` y `location.assign/replace`; no ejecuta el JavaScript
de esos manejadores. Los manejadores complejos o ambiguos quedan para intervención.

Cuando se observan varias marcas distintas y hay una coincidencia única con el
ticket, se sigue ese control automáticamente. No se prueban otras marcas. Ante
ambigüedad aparecen **Abrir marca** y **Continuar revisión con IA**. Un enlace
claro de facturación tiene prioridad sobre un logo de cabecera. El permiso inicial
del recorrido cubre también el proveedor externo. El destino siempre procede de la
página actual; ni el usuario del modelo ni el modelo pueden inventar una URL de
navegación mediante la herramienta. Una ventana nueva se abre en el mismo contexto
aislado, sin propagar ventanas emergentes.

Al llegar a un formulario con campos de facturación reconocidos, aparece
**Guardar portal de la marca**. Confirma este botón solo si corresponde al comercio.
El enlace observado se guarda cifrado durante 30 días, separado por usuario,
marca, RFC del emisor cuando esté disponible y dominio de origen. No se guarda el
portal de una marca para todas las marcas de un mismo grupo o RFC. Se ignoran
acentos y puntuación al comparar. También se reconocen etiquetas extendidas con
el nombre como palabras completas, como «Dominos» y «Domino’s Pizza»; una
asociación recordada siempre requiere la confirmación humana del formulario.

En nuevos tickets de ese comercio se propone directamente el enlace recordado,
conservando la liga impresa original. Antes de visitarlo se requiere el permiso
del nuevo ticket; guardar la liga no guarda autorizaciones, cookies o sesiones.
**Usar enlace del ticket** permite volver al recorrido original. `/portales`
muestra enlaces propios y **Olvidar enlace** los retira sin borrar perfiles o
tickets. Al vencer un enlace guardado se vuelve al origen del ticket.

Solo se recuerdan URLs HTTPS observadas y autorizadas. Se descartan enlaces con
parámetros de sesión, tokens o referencias de compra y se conservan únicamente
parámetros públicos acotados de selección de marca/idioma. Un formulario dentro de
un iframe puede recordar la dirección estable de ese formulario, si su dominio
fue autorizado. Los botones viejos o una página que cambió no pueden guardar una
dirección distinta sin una revisión nueva. La compatibilidad de cada portal real
sigue requiriendo comprobación. Esta entrega no rellena ni envía formularios.

## Límites y comprobaciones

- Se usa un único agente; Playwright es una herramienta, no otro agente.
- Hasta ocho pasos y 180 segundos por sesión pública; el backend conserva la
  revisión 150 segundos. Hasta dos contextos de navegador a la vez.
- Cada ejecución conserva los límites existentes de chat: pasos del SDK, tokens,
  tiempo y cuota diaria. No se elevan automáticamente ni se ocultan llamadas extra.
- Referencias observadas de un solo uso; una página cambiada exige nueva revisión.
- Solo peticiones HTTPS GET dentro del recorrido autorizado (máximo 12 orígenes).
  IPs privadas, métodos POST, WebSockets, descargas y service workers se bloquean.
  Las conexiones HTTP verifican DNS público y fijan IP, Host y SNI para TLS.
- El navegador no recibe secretos fiscales. Su tráfico pasa por el filtro del
  servidor; la salida directa del navegador usa un proxy local que no da servicio.
  El contenedor usa UID 10001, raíz de solo lectura, límites de memoria/procesos y
  ninguna capacidad adicional. No se afirma que Chromium use su sandbox interno.
- Los recursos de Google Tag Manager, Google Analytics y DoubleClick se omiten
  como medición/publicidad; no piden permisos ni frenan la navegación. Se compara
  el hostname exacto o un subdominio de esas familias, sin bloquear otros servicios
  de Google ni proveedores de bibliotecas de formularios por semejanza de nombre.
  Los recursos funcionales de otro dominio quedan cubiertos por el permiso del
  recorrido. Los permisos antiguos por dominio y la API web mantienen su alcance
  original hasta una autorización nueva y explícita.
- Se priorizan enlaces observados que mencionen facturación, billing, invoice o
  CFDI, incluso si están al final de la página. No hay una receta fija por comercio.
- Imágenes y medios se omiten en esta fase basada en DOM. CAPTCHA, login, páginas
  solo gráficas, controles incompatibles o ambigüedad
  requieren intervención humana. No hay garantía de compatibilidad universal.

Las pruebas locales usan Chromium real con páginas ficticias y el SDK real con
respuestas de modelo programadas. **No se ha probado la navegación de producción
con los portales reales ni se han realizado llamadas nuevas de OpenAI de pago.**

## Si aparece «No se pudo revisar la página pública»

Ese mensaje no identifica por sí solo si falló la comunicación interna, Chromium,
DNS, TLS o el servidor del comercio. No implica que la CSF o el ticket sean ilegibles.
Antes de repetir una ejecución de IA, usa el diagnóstico desde Termius:

```bash
(
  set -e
  cd /opt/lilyapp-agent
  git pull --ff-only
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml exec -T agent python - --component connection < scripts/check_browser.py
  read -r -p "Dirección del portal del ticket: " portal_url
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml exec -T browser python - --component portal --url "$portal_url" < scripts/check_browser.py
)
```

No requiere reconstruir imágenes ni reiniciar servicios: el script nuevo se pasa
por stdin a los contenedores existentes. El primer paso verifica `/health` y la
credencial interna mediante una URL local que se rechaza **antes de abrir páginas**.
Si este paso falla, se detiene el bloque; primero hay que restablecer la conexión
entre servicios. El segundo paso arranca Chromium con una página ficticia, sin
comercios, y comprueba un único GET HTTPS del portal indicado. No usa OpenAI,
documentos, perfil fiscal ni identificadores de la compra. Elimina consultas y
fragmentos del enlace y no sigue redirecciones; muestra solo el dominio del destino.
Nunca desactiva TLS ni permite IPs privadas para hacer pasar la comprobación.

Los resultados distinguen DNS, certificado, tiempo de espera, conexión rechazada,
estado HTTP y arranque local de Chromium. No imprimen cuerpos de las páginas,
excepciones, cookies ni claves. Un HTTP 200 solo confirma que respondió la dirección;
no valida el ticket ni garantiza que el formulario sea compatible. La comprobación
no modifica perfiles, tickets, cuotas, credenciales o configuración de la aplicación.

## API para extender la interfaz web

Con autenticación, origen válido y CSRF:

- `POST /api/billing/tickets/{id}/portal-permissions` con `{}` crea la revisión del
  permiso. `origins` opcional solo admite dominios realmente observados en el ticket
  o su última página; nunca una URL inventada por el modelo.
- `POST /api/billing/portal-permissions/{id}/decision` recibe `{"approve":true}` o
  `{"approve":false}` tras la decisión humana; registra o revoca el permiso.
- `POST /api/billing/tickets/{id}/browser`: abre o recupera la revisión propia;
  devuelve 403 si falta el consentimiento, incluso para la inspección estática.
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


### API de enlaces recordados

Con autenticación y CSRF para mutaciones:

- `GET /api/billing/portals`: solo enlaces del usuario autenticado.
- `POST /api/billing/tickets/{id}/portal-bookmark`: `confirmed: true` y el
  `observation_id` actual; la URL se deriva en el servidor, no se acepta en el JSON.
- `POST /api/billing/tickets/{id}/portal-selection`: `confirmed: true` y
  `use_saved: true/false`; cambia el punto de entrada sin abrir páginas.
- `DELETE /api/billing/portals/{id}` con `confirmed: true`: olvida un enlace propio.

No hay herramienta del modelo que confirme o guarde el enlace por el usuario.
