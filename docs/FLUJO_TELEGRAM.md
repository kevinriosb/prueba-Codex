# Una solicitud activa y menús de Telegram

## Reglas de uso

1. `/start` y `/menu` muestran la solicitud activa y las opciones **Continuar**,
   **Nueva solicitud**, **Mis solicitudes**, **Mi perfil fiscal** y **Cerrar o
   cancelar**. Cuando no existe una solicitud activa, se indica qué archivo falta.
2. La CSF se confirma una vez por perfil. Cambiar de ticket, chat o solicitud no
   vuelve a pedirla ni borra el perfil. Los cambios fiscales se revisan aparte.
3. Cada solicitud tiene un identificador corto. El comercio y la referencia se
   muestran cuando ya se conocen; antes de analizar no se inventa su contenido.
4. Un archivo recibido no autoriza analizarlo ni enviarlo. Guardarlo, elegir su
   tipo y cambiar de solicitud se resuelven con botones, sin generación de IA.
5. Otro ticket queda activo automáticamente. La solicitud anterior se pausa y
   sigue en **Mis solicitudes**; no exige cancelarla ni anula envíos al comercio.
   La subida no dispara IA: se conserva el permiso de análisis del archivo nuevo.
   Si después se identifica la misma compra, se retoma su solicitud existente.
6. Si se pidió otra foto mediante el botón del ticket, la siguiente subida pregunta
   **Otra foto del mismo ticket** o **Es otro ticket**. Nunca adjunta una foto
   distinta al ticket anterior por conservar un estado viejo del chat. Una
   indicación explícita `/csf` o **Actualizar CSF** tiene prioridad sobre esa
   petición de foto anterior; no cambia la solicitud activa.
7. La misma imagen de la misma solicitud reutiliza los datos y conserva el punto
   de navegación. Otra foto de la misma compra puede detectarse como duplicada
   después del análisis autorizado; se informa y se reutiliza la solicitud real.
8. `/nuevo`, `/nuevo_ticket`, «Empezar con otra solicitud» y «Empieza de 0 un nuevo
   ticket» pausan la activa y abren una nueva sin pedir cancelar. Después esperan
   el archivo nuevo y limpian el ticket, la captura y el navegador seleccionados
   en el chat. Conservan los registros, perfiles, permisos históricos y cuotas.
9. `/nuevo_chat` reinicia únicamente la conversación de IA. No es otro ticket,
   ni cancela una solicitud. Un límite de ese chat no bloquea los menús nativos.
10. Los botones de otra solicitud requieren cambiar explícitamente a ella antes
    de actuar. No se ejecuta automáticamente la acción del botón anterior tras
    cambiar: se presenta el estado real para revisar el paso correspondiente.
11. Una cancelación se confirma con un botón. Retira aprobaciones pendientes,
    revoca permisos del portal, cierra su sesión pública y detiene el seguimiento.
    Conserva los datos. No anula un correo enviado ni una factura emitida. Un envío
    incierto permite pausar y consultar; no permite afirmar que se canceló.
12. Una pausa conserva la solicitud y sus datos para retomarla. Pausa su atención
    interactiva; las consultas de correo previamente autorizadas pueden continuar.
    Sus avisos identifican la solicitud y no cambian la que se está atendiendo.
13. Una solicitud pausada o cancelada se consulta en **Mis solicitudes**. Retomarla
    exige una elección explícita; los permisos retirados necesitan autorización
    nueva. El modelo no puede seleccionar otra solicitud usando el historial.
14. Se admiten foto e imagen/PDF como documento. Un video, animación o Live Photo
    enviado como video recibe «Recibí un video. Envía el ticket como foto o PDF».
    No se descarga ni se manda a IA como si fuera una imagen. Responder «Es este»
    citando una foto/PDF recupera ese adjunto; citar un video explica su formato.

## Del inicio al cierre

| Situación | Qué muestra el bot | Qué permite continuar |
|---|---|---|
| Sin perfil | Crear o completar perfil en Telegram | CSF, análisis autorizado y confirmación fiscal |
| Perfil confirmado, sin ticket | Menú y petición de foto/PDF | Subir el ticket |
| Archivo nuevo con solicitud activa | Identidad anterior y tres opciones | Elegir qué solicitud atender |
| Paso 1: ticket recibido | Identidad y autorización de lectura | Botón de análisis; no un «sí» escrito |
| Paso 2: medio de facturación | Portal o correo observado | Recorrido público autorizado o revisión del contacto |
| Paso 3: datos | Campos reales del portal o borrador de correo | Leer la foto según esos campos y completar solo lo que falta |
| Paso 4: revisión | Datos concretos y controles de confirmación | Enviar un paso del portal, llenar sin red o aprobar un correo |
| Portal enviado | Respuesta real, archivos disponibles o envío sin confirmar | Consultar sin repetir; completar los nuevos campos si los pide |
| Correo enviado | Esperando factura e identidad de solicitud | Consultar respuesta o autorizar seguimiento |
| Factura recibida | XML/PDF y resultado de comprobación | Consultar SAT solo con su autorización específica |
| Factura validada | Completada, descargas y menú | Iniciar otra solicitud conservando el perfil |
| Error, vencimiento o ambigüedad | Causa conocida y paso recuperable | Resolver ese dato o volver al menú, sin reiniciar el perfil |

Los números son una guía de la solicitud, no una receta para todos los portales.
El navegador sigue investigando los controles actuales de cada sitio. El llenado
con red cerrada **no envía el formulario ni emite la factura**. No se añade una
secuencia específica para marcas o proveedores. CAPTCHA, login o controles no
compatibles pueden requerir intervención humana.

El envío real y la corrección con una foto de un solo campo se describen en
[Facturación desde Telegram](FACTURACION_PORTAL.md). Cada pantalla del portal
se revisa y confirma por separado; el llenado aislado sigue disponible.

## Datos visibles y web oficial

La primera lectura actual (`portal-fields-v3`) también obtiene los datos básicos
visibles: importe, fecha, folio/Cuenta, moneda y RFC emisor. La imagen no se relee
si el portal solo pide datos que ya están disponibles. Los campos adicionales se
extraen cuando se observa el formulario. Un dato ausente se pide por nombre con
**Completar campo**: se acepta texto normal o foto cercana con análisis autorizado;
no exige `/dato_ticket`, reenviar todo el ticket ni actualizar la CSF.

El permiso inicial actual dice que cubre analizar el ticket y buscar/recorrer la
web oficial con IA. No incluye enviar formularios fiscales. Sin una liga fiscal
ni un correo expresamente indicado, se usa la web oficial impresa con evidencia;
si falta, una búsqueda web real con fuentes. No se deduce el dominio del nombre
ni se elige un resultado ambiguo. Una encuesta se descarta, también en solicitudes
guardadas, y su URL con datos no se ofrece como portal.

La navegación sigue los controles reales hasta Facturación y el formulario. Las
solicitudes con permisos antiguos conservan su alcance y ofrecen **Buscar facturación
oficial** o **Escribir web oficial** para autorizar el recorrido público nuevo. Si
no se verifica el sitio, se pide su liga. Los resultados de búsqueda se reutilizan
24 horas por cuenta; `/limites` muestra sus consultas y el límite vigente. La búsqueda
recibe solo el nombre público del comercio, sin CSF, RFC receptor, correo, foto o
datos de la compra. La confirmación del envío real al comercio sigue siendo privada.

## Estado y controles del servidor

`TelegramWorkflow` concentra menús, cambios, pausas, cierre y protección de botones.
`active_ticket_id` identifica la solicitud elegida; `workflow_mode` distingue la
espera de un archivo del chat general. La migración inicial toma la selección
previa propia, sin borrar registros ni cambiar la base de datos o la clave.

Cada elección se guarda cifrada con propietario, archivo, solicitud de origen,
objetivo, caducidad y uso único. Un menú antiguo, usado, de otra cuenta o de un
archivo vencido no puede seleccionar un archivo distinto. Reenvíos del webhook
siguen usando la deduplicación y el bloqueo existentes por cuenta. El control de
selección también se aplica a herramientas de IA, comandos manuales y botones.

La foto de reemplazo ya guardada se convierte en el documento del ticket sin
crear una segunda copia ni otro ticket provisional. Se conservan su vencimiento
y sus bytes ya validados: no se aplica otra compresión JPEG que cambie la huella
o degrade la imagen. El archivo recibido por primera vez mantiene su validación.
La verificación de la foto, los campos y los permisos de llenado sigue intacta.
No se transfieren análisis, datos o autorizaciones de una solicitud a otra.

Los menús, estado, perfil y listados siguen disponibles bajo su presupuesto de
consultas aunque se haya agotado el presupuesto diario de acciones. Análisis,
cuotas de tokens y acciones externas mantienen sus autorizaciones y límites.

Las regresiones usan el webhook, el almacenamiento y el SDK reales con documentos,
modelo, Gmail y SAT simulados. Incluyen principio a fin del envío autorizado,
recepción XML/PDF y validación con SAT simulado. No se envían correos reales, no
se hacen llamadas de IA de pago ni se demuestra compatibilidad universal de sitios.

## Validación y despliegue

Pasaron **780 pruebas completas** (390,09 s), **116 regresiones de los flujos
afectados** y un caso adicional del SDK que verifica proveedores abiertos como
fuentes reales por la búsqueda web. También pasaron compilación de Python,
detección de nombres inválidos y comprobación de espacios.

Los casos incluyen pausa automática sin anular un correo enviado, campos básicos
en la primera lectura, importe faltante sin botón de envío, captura manual y
recorte del importe, descarte de encuesta incluso en solicitudes anteriores,
fuentes reales frente a dominios inventados, sitio ambiguo, web impresa, ingreso
manual de la web y ahorro de una segunda lectura si todos los datos ya existen.
El SDK, el webhook, el almacenamiento y Chromium son reales; las páginas, el
modelo, correo y SAT son simulados. Las pruebas no emiten facturas reales.

Otras **65 regresiones** (90,30 s) comprueban también la caducidad: una caché
vencida permite consultar de nuevo el mismo comercio y los datos de la primera
lectura siguen utilizables después de vencer la foto, sin pedir otra subida.
Una foto de reemplazo pendiente de verificar mantiene su protección original.

Esta revisión conserva el motor Playwright, frontend, dependencias, claves,
esquema y archivos Docker/Compose. No despliega ni altera contenedores del VPS.

Actualizar ambos servicios existentes aplica los menús y la corrección de cierre,
conservando sus volúmenes y credenciales:

```bash
(
  set -e
  cd /opt/lilyapp-agent
  git pull --ff-only
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml build agent browser
  docker compose -f compose.hostinger.yaml -f compose.browser.yaml up -d --no-deps --wait --wait-timeout 180 agent browser
  curl --fail --silent --show-error --max-time 20 https://agente.lilyapp.mx/health
)
```

Después, `/start` o `/menu`. No se borra ni reinicia la información existente.

La repetición completa posterior pasó 734 pruebas (353,23 s). Las 48 pruebas
posteriores de ciclo de recursos, reemplazo y flujo nativo pasaron (46,71 s).
GitHub publica ahora el nombre y detalle de cada fallo como anotación a partir
del informe JUnit; la descarga de logs no estuvo disponible en este entorno.
No se silencian advertencias de recursos ni se cambian los límites del portal.

La revisión posterior de ciclo de recursos pasó 737 casos completos (357,91 s).
Después pasaron 62 comprobaciones de flujo, perfiles, archivos y cierre (33,97 s),
incluida la prioridad de una CSF explícita sobre una petición vieja de foto.
