# Facturación por correo: guía para usar y configurar desde el iPad

La actualización amplía el agente existente. Conserva el chat, autenticación, cálculo, reloj, historial y aprobaciones anteriores. Agrega un módulo fiscal; no se ha contratado alojamiento, conectado una cuenta real de Gmail, registrado un webhook real ni enviado solicitudes de factura durante su implementación.

## Qué está implementado y qué falta conectar

| Componente | Código incluido | Comprobación y límites |
| --- | --- | --- |
| Perfil fiscal | CSF en PDF/foto, extracción estructurada, uso de CFDI y correo elegidos por el usuario, resumen y confirmación; varios perfiles y actualización mediante otra CSF | Documentos ficticios, extracción simulada y Runner real con respuestas programadas. No se verificó la lectura de una CSF real con un modelo de pago |
| Protección de datos | AES-256-GCM por registro, clave privada del servidor, propietario/tipo/ID autenticados con el cifrado, eliminación y vencimiento de originales | Cifrado real, aislamiento, intercambio de ciphertext rechazado y pruebas de borrado |
| Tickets | Emisor, fecha, folio, total, sucursal, código, portal y correo; selección de canal y deduplicación por usuario | No se obedecen instrucciones del documento ni se visitan sus enlaces. Un correo de soporte no se autoriza por contener la palabra facturación en su dirección |
| Gmail | OAuth 2.0 con PKCE, tokens cifrados por usuario, renovación, envío MIME y lectura de respuestas del hilo autorizado | Protocolo HTTP probado con transporte simulado. Hace falta configurar Google y conectar cada buzón |
| Aprobación de envío | Vista del destinatario, datos, texto y adjuntos; autorización de un solo uso, vencimiento, protección contra repetición y datos modificados | Desactivado por defecto. Un resultado incierto bloquea reintentos incluso tras reiniciar o caducar el borrador |
| CSF adjunta | Petición expresa del comercio registrada y autorización específica del usuario, además de la aprobación final del correo | Nunca se adjunta por defecto. Si el original ya venció debe recibirse otra copia |
| Respuestas | Asociación por propietario, destinatario, hilo e identificador; PDF/XML, validación de coincidencias y descarga privada | Una respuesta de otro remitente/hilo exige intervención. No se asocia una factura solo por un asunto parecido |
| CFDI 4.0 | Parser XML sin DTD/entidades, versión, timbre/UUID y coincidencia de receptor, régimen, uso, CP, nombre, importe y emisor cuando existe en el ticket | No incluye validación XSD completa ni verificación criptográfica de sellos. La presencia de un PDF no valida un CFDI |
| Vigencia SAT | Adaptador SOAP con destino HTTPS fijo, desactivado por defecto; autorización expresa para compartir RFC, UUID e importe | Transporte simulado. No se hizo una consulta fiscal real. Consulta de vigencia y verificación criptográfica son comprobaciones distintas |
| Seguimiento | Consulta manual y seguimiento periódico opcional por ticket, con política revisada y autorizada, límite total y parada tras tres errores consecutivos | Sin recordatorios automáticos ni nuevos correos de seguimiento. Avisos en Telegram para su propio usuario y estados disponibles en la web |
| Telegram | Conversación libre con el mismo agente, historial por persona, webhook con secreto, lista de invitados, PDF/foto y botones de consentimiento y aprobación | Runner real, servicios simulados y aislamiento probados. La conexión real de un bot nuevo sigue requiriendo configuración |
| Portales de comercios | Registro de adaptadores del servidor y prioridad sobre correo cuando hay un adaptador compatible autorizado | No hay adaptadores de portales comerciales instalados. No se automatiza un portal arbitrario encontrado en un ticket |
| WhatsApp | La lógica fiscal está separada del canal y puede reutilizarse | Adaptador de WhatsApp pendiente; no hay un número o integración de WhatsApp activados |

## 1. Necesitas una dirección web del servidor

El código en GitHub no ejecuta el backend. Para abrir la aplicación en Safari o recibir webhooks de Telegram hace falta una URL HTTPS accesible. Puedes usar el despliegue posterior en Railway descrito en el README, con un volumen persistente en `/data`. Su creación y cualquier contratación siguen necesitando autorización.

La aplicación no necesita las integraciones fiscales para seguir funcionando como chat general. El apartado Facturación permanece desactivado hasta configurar el cifrado.

## 2. Activa el cifrado

1. Con la aplicación accesible, inicia sesión y abre **Facturación · Perfiles y tickets**.
2. Si todavía no hay datos cifrados, pulsa **Generar clave nueva de cifrado** y **Copiar clave nueva**. Se generan 32 bytes aleatorios mediante Web Crypto en tu navegador: no se recupera una clave existente del servidor, no se envía la clave generada por una API ni se almacena en el navegador.
3. Pega el resultado en la variable privada **`BILLING_ENCRYPTION_KEY`** del servidor y reinicia o vuelve a desplegar el servicio. En el entorno de Codex utiliza **Variables de entorno**, con un valor directo para el proceso.
4. Guarda una copia en tu gestor de contraseñas. No la envíes por chat, Telegram, correo ni GitHub. No cambies esta clave para actualizar un perfil: perderla impide descifrar la información existente.

Si el servidor ya tiene datos cifrados pero falta su clave, el generador se oculta: debes recuperar la clave original.

Alternativa desde una terminal de confianza:

```sh
python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

Ese comando muestra un secreto nuevo en tu terminal; cópialo únicamente a la configuración privada del servidor y a una copia segura. No lo ejecutes para reemplazar la clave de una base de datos existente.

**Esta clave cifra documentos y perfiles; no es la clave de OpenAI.** La credencial de OpenAI sigue configurándose como `AI_API_KEY` en el backend. Para este entorno de Codex se conserva el arranque probado que utiliza `AI_API_KEY_CHECK` como valor de `AI_API_KEY` solo para ese proceso.

## 3. Conecta Gmail con autorización de Google

Desde Safari en el iPad puedes administrar [Google Cloud Console](https://console.cloud.google.com/). No se necesitan contraseñas de Gmail ni acceso a la cuenta desde este chat.

1. En un proyecto de Google Cloud, habilita **Gmail API**.
2. Configura la pantalla de consentimiento OAuth. Para una prueba personal, agrega tu cuenta como usuario de prueba. Una aplicación para otros usuarios puede requerir revisión/verificación de Google para los permisos de Gmail; no se ha realizado ese proceso.
3. Crea un cliente OAuth de tipo **Aplicación web**.
4. Agrega como URI de redirección el origen HTTPS real de tu aplicación seguido de **`/api/billing/gmail/callback`**. Debe coincidir exactamente con la variable del servidor; no copies un dominio de ejemplo.
5. En las variables privadas del servidor configura:

| Variable | Valor que debes introducir |
| --- | --- |
| `GMAIL_CLIENT_ID` | ID del cliente OAuth de Google |
| `GMAIL_CLIENT_SECRET` | Secreto del cliente, disponible directamente para el proceso |
| `GMAIL_REDIRECT_URI` | URI HTTPS de retorno configurada en Google |
| `APP_ALLOWED_HOSTS` | Dominio de tu aplicación sin esquema ni ruta, conservando los hosts de salud necesarios |
| `BILLING_LIVE_EMAIL_ENABLED` | Mantén `false` para revisar borradores. Usa `true` solo cuando quieras permitir los envíos que confirmes |

6. Reinicia la aplicación. En **Facturación → Conectar Gmail**, elige tu cuenta y revisa los permisos: envío y lectura (`gmail.send` y `gmail.readonly`). El backend almacena los tokens cifrados; no los devuelve al navegador.
7. Para conectar el buzón de un usuario de Telegram, usa **`/conectar_gmail` desde ese chat** y autoriza el enlace de Google. La cuenta se vincula a ese usuario de Telegram, no al propietario de la sesión web.

En el modo de pruebas de Google, los tokens de renovación pueden caducar a los siete días con permisos de Gmail; en ese caso vuelve a conectar. No se crean automáticamente cuentas de Google ni servicios de pago.

**Desconectar Gmail** elimina del agente los tokens de esa cuenta. Para revocar también el permiso otorgado a Google, utiliza las conexiones de terceros en la configuración de tu cuenta de Google.

El correo indicado en el perfil es el destino solicitado para PDF/XML. El buzón conectado es el emisor y dirección de respuesta de la solicitud. Si el comercio envía una factura en un hilo nuevo o a un buzón no conectado, se requiere revisión manual: no se adivina la asociación.

## 4. Configura Telegram

Para el VPS con AlmaLinux y Docker utiliza la [guía de Telegram en Hostinger](TELEGRAM.md).
Incluye un asistente compatible con Python 3.9, token oculto, verificación de la
primera cuenta por código y administración manual de invitados. Los comandos
`uv` siguientes corresponden a una instalación de desarrollo con Python 3.12.

1. Crea o elige un bot en BotFather. Copia su token únicamente a **`TELEGRAM_BOT_TOKEN`** en las variables privadas del servidor.
2. Define **`TELEGRAM_WEBHOOK_SECRET`**: un valor aleatorio de 32 a 256 caracteres formado por letras, números, guion o guion bajo. No es el token de BotFather y no se comparte con usuarios.
3. Configura **`TELEGRAM_ALLOWED_USER_IDS`** con los IDs numéricos de las cuentas autorizadas, separados por comas. El nombre `@usuario` no sustituye al ID. No habilites usuarios desconocidos para probar datos reales.
4. Con el backend HTTPS operativo, registra el webhook de **ese bot** en `/integrations/telegram/webhook`, incluyendo el `secret_token`. El repositorio contiene una herramienta para hacerlo sin poner el token en la línea de comandos:

```sh
uv run --frozen python scripts/configure_telegram.py --url ORIGEN_HTTPS_REAL
```

El comando anterior solo valida la configuración. Cuando autorices registrar el webhook:

```sh
uv run --frozen python scripts/configure_telegram.py --url ORIGEN_HTTPS_REAL --apply
```

Reemplaza `ORIGEN_HTTPS_REAL` por la dirección real, sin rutas. Desde el iPad puedes solicitar ejecutar ese registro en este entorno una vez configuradas las variables y autorizado el cambio. El script no se ejecuta automáticamente al arrancar ni durante las pruebas.

5. En el chat privado del bot pulsa **Iniciar** o envía **`/start`**. Los grupos y los usuarios fuera de la lista se ignoran.

### Recorrido en Telegram

- Antes del primer perfil, el bot pregunta si un archivo desconocido es CSF o ticket. Con una CSF recibida o perfil confirmado, los nuevos archivos se tratan como tickets. Para otra constancia indica `/csf` en la descripción. Elegir el tipo no autoriza el análisis.
- Sin perfil: puedes guardar primero el ticket. Quedará pendiente mientras envías la CSF, autorizas su análisis y confirmas el perfil. Después retomas el ticket guardado, sin otra subida.
- `/documentos` permite recuperar un archivo vigente y corregir su tipo. «Es un ticket» corrige el último recibido; no autoriza automáticamente una nueva extracción.
- Tras la extracción: elige expresamente un uso de CFDI. **`/usos`** muestra el catálogo. Después introduce el correo de recepción y confirma el resumen con el botón de guardar.
- Envía el ticket para el perfil activo, autoriza su análisis y revisa el correo identificado en el original. Prepara la solicitud y confirma o rechaza su envío.
- **`/perfiles`** permite elegir o eliminar perfiles. Para agregar otro, envía una constancia con la descripción **`/csf`**; para actualizar uno utiliza **`/csf ID_DEL_PERFIL`**.
- **`/tickets`** muestra solicitudes, archivos de factura y botones para consultar el buzón o autorizar el seguimiento.
- Si el comercio pidió la constancia completa, registra esa petición mediante **`/requiere_csf ID_TICKET descripción de la petición`**. Después **`/preparar_con_csf ID_TICKET`** autoriza incluirla en un borrador; aún debes aprobar el correo final.
- Una solicitud ya enviada no se repite. Se permite un complemento aprobado con CSF si el comercio lo pidió y no se compartió antes; no es una política de recordatorios.

Telegram ofrece conversación libre con el mismo agente, además del flujo fiscal
guiado. `/nuevo` inicia otra conversación y `/historial` recupera mensajes propios.
Los documentos exigen autorización de análisis y los envíos una revisión con
botón, incluso si el texto pide omitirlos. Los perfiles de la web y de cada cuenta
de Telegram están aislados; no existe vinculación de identidades entre canales ni
adaptador de WhatsApp.

## 5. Envía tu primer ticket desde la web

1. Abre **Facturación**, sube la CSF y marca el consentimiento de análisis con OpenAI.
2. Revisa RFC, nombre, CP y régimen. Si falta algo o el documento es ilegible, envía una copia más clara. No se completan campos por suposición.
3. Elige uso de CFDI y correo. Solo entonces pulsa **Confirmar y guardar perfil**.
4. Selecciona un perfil confirmado y sube el ticket. Todavía no se envía ningún correo.
5. Revisa su canal. Un portal solo tiene prioridad si existe un adaptador del servidor compatible y autorizado. No se visitan automáticamente las URL del documento.
6. Para correo, revisa el ticket original o la fuente oficial registrada y confirma que el destinatario se usa para facturación. Los correos genéricos no se habilitan automáticamente.
7. Pulsa **Preparar correo para revisión**. Comprueba destinatario, texto, datos fiscales y adjuntos. Solo incluye el ticket por defecto.
8. Si el comercio exige la CSF, registra su petición y utiliza el botón de autorización específico. La aprobación anterior debe rechazarse si vas a cambiar los adjuntos.
9. Con Gmail conectado y el envío habilitado, pulsa **Confirmar y enviar este correo**. El resultado correcto es **Esperando respuesta**, no Facturación completada.

La verificación de un destinatario del ticket es una **revisión manual del usuario**, no una prueba de propiedad del buzón. El catálogo opcional `BILLING_VERIFIED_CONTACTS_JSON` permite contactos revisados por el administrador en una fuente oficial, vinculados por RFC del emisor. No se puede crear ese catálogo desde un ticket, una respuesta de correo ni una herramienta del modelo. No uses direcciones de ejemplo como destinatarios reales.

## 6. Seguimiento y estados

El historial de estados conserva `TICKET_RECIBIDO`, `DATOS_EXTRAIDOS`, `PENDIENTE_CONFIRMACION`, `CORREO_PREPARADO`, `CORREO_ENVIADO`, `ESPERANDO_RESPUESTA`, `FACTURA_RECIBIDA`, `FACTURA_VALIDADA`, `FACTURACION_COMPLETADA`, `REQUIERE_INTERVENCION` y `ERROR`.

**Consultar respuestas en Gmail** solo procesa mensajes cuyo remitente e hilo coinciden con la solicitud. Las instrucciones del correo son texto no confiable: no autorizan destinatarios nuevos, recordatorios ni adjuntos.

Si llega un XML, se coteja con los datos fiscales aprobados para ese envío, aunque el perfil se haya actualizado después. Un PDF sin XML queda pendiente de intervención. Para consultar vigencia ante el SAT configura **`BILLING_SAT_ENABLED=true`** y autoriza en la interfaz compartir RFC, UUID e importe. Nunca se envía la CSF al SAT.

`FACTURA_VALIDADA` significa que el XML pasó las comprobaciones de coincidencia implementadas y la consulta de vigencia del SAT resultó satisfactoria. `FACTURACION_COMPLETADA` además requiere PDF y XML disponibles. **No significa que se hayan validado criptográficamente los sellos o todo el esquema XSD del SAT.** El resultado conserva ese alcance explícitamente.

Para seguimiento periódico configura **`BILLING_BACKGROUND_TRACKING_ENABLED=true`**. Cada usuario debe revisar y activar la política para cada ticket. Por defecto consulta cada 3600 segundos, con un máximo de 72 consultas por ticket; tres errores consecutivos detienen el proceso. El máximo no se reinicia al detener y reactivar. El proceso no consulta al SAT ni envía otros correos automáticamente. En Telegram avisa únicamente de cambios; en la web puedes actualizar el estado. El servidor debe permanecer activo para ejecutar el seguimiento; al reiniciar conserva la política y los contadores en SQLite.

Si hay un envío con resultado incierto, **revisa Gmail antes de cualquier repetición**. La aprobación queda consumida y no se intenta enviar de nuevo por un timeout, reinicio o vencimiento del borrador.

## 7. Retención, eliminación y límites

| Dato o límite | Valor inicial |
| --- | --- |
| PDF/fotografías originales de CSF y ticket | 24 horas; configurable entre 1 y 168 |
| PDF y XML de facturas recibidas | 30 días; configurable hasta 90 |
| Perfil fiscal, tokens y trazabilidad de solicitudes | Cifrados hasta su eliminación por el usuario |
| Limpieza de archivos vencidos | Al acceder al módulo y cada cinco minutos mientras corre el servidor |
| Archivo subido | Hasta 8 MB; PDF sin contraseña, máximo cinco páginas; imágenes JPEG/PNG/WebP con límite de píxeles |
| Adjuntos combinados de una solicitud de correo | 6 MB |
| Análisis de documentos por usuario/día UTC | 10; una llamada por documento, sin reintentos automáticos |
| Salida máxima del modelo por análisis | 1400 tokens; el proveedor también factura entrada/documentos |
| Documentos activos por usuario | 40 |
| Perfiles por usuario | 10 |
| Actualizaciones de Telegram por usuario/día UTC | 100 |
| Reintentos automáticos de envío | Ninguno |
| Recordatorios de facturación por correo | No implementados ni activados |

En **Archivos temporales** puedes eliminar originales antes de su vencimiento. Eliminar un perfil elimina sus solicitudes y archivos asociados; las aprobaciones que pierdan adjuntos o cambien de perfil quedan bloqueadas. Se mantienen auditorías mínimas con identificadores opacos y estados, sin RFC, correos, documentos ni contraseñas.

El borrado afecta al almacenamiento de esta aplicación. No elimina las copias que el usuario haya subido a Telegram, los correos ya enviados a Gmail/comercios, descargas o copias de seguridad externas. La aplicación no promete borrar físicamente todas las versiones de un disco o backup. El historial normal del chat mantiene su arquitectura anterior; utiliza el módulo fiscal para documentos y datos fiscales, cuyas herramientas devuelven al chat solo identificadores y estados.

La extracción pide `store=false` a OpenAI. Esto evita guardar una respuesta recuperable mediante Responses, pero no garantiza ausencia de toda retención del proveedor. El análisis del documento se autoriza expresamente y queda sujeto a las condiciones de su API.

La validación de formatos y la eliminación de metadatos de imágenes no son un antivirus completo. Antes de exponer la aplicación a usuarios no confiables conviene añadir análisis antimalware aislado y controles de almacenamiento/operación apropiados al despliegue.

## 8. Pruebas sin usar servicios reales

```sh
uv sync --frozen
uv run --frozen pytest -q
```

Las pruebas aíslan las credenciales reales y usan RFC, CSF, tickets, facturas y buzones ficticios. Hay pruebas con Runner real, transporte HTTP simulado de Gmail/SAT/Telegram y cifrado real. No se pide un API key ni se envían documentos privados.

Para las pruebas de interfaz opcionales:

```sh
uv sync --frozen --group browser
uv run --group browser playwright install chromium
uv run --group browser python scripts/browser_smoke.py
uv run --group browser python scripts/billing_browser_smoke.py
```

La segunda prueba recorre el flujo fiscal en emulación táctil de iPad: autorización de análisis, selección explícita del uso, perfil, ticket, revisión del correo, rechazo/envío simulado, persistencia y cierre de sesión. Si existe Chromium del sistema, configura `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` en lugar de descargarlo. Los servidores de estas pruebas son locales, temporales y no constituyen un despliegue para tu iPad.

Si realizas pruebas de integración después, primero configura las variables y destinos HTTPS necesarios. En Codex, los destinos de salida son `api.openai.com`, `oauth2.googleapis.com`, `gmail.googleapis.com`, `api.telegram.org` y, únicamente para la consulta autorizada, `consultaqr.facturaelectronica.sat.gob.mx`. Conserva cualquier otro dominio que el entorno necesite. Nunca publiques tokens en el repositorio ni pases valores secretos como argumentos de comandos.
