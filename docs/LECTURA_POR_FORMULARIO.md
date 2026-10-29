# Leer el ticket según el formulario observado

Los tickets nuevos en Telegram se procesan en este orden:

1. La autorización de análisis explica las lecturas con OpenAI. La primera
   identifica únicamente el comercio y su portal o correo impreso de facturación.
2. Con el permiso público del ticket, el navegador encuentra el formulario real.
   No se fija una secuencia por dominio o marca.
3. La segunda lectura pide los campos de compra de ese formulario y los datos
   mínimos de identidad de la compra: emisor, fecha, referencia e importe.
   RFC receptor, nombre, CP, régimen, uso y correo proceden del perfil confirmado.
4. Telegram muestra valores y permite completar o corregir los campos de compra
   con botones y `/dato_ticket`. Un número de tienda conserva los ceros iniciales;
   no se deduce de marca, dirección ni folio. Una fecha se adapta a `dd/mm/aaaa`
   cuando el campo lo indica; un input de fecha conserva ISO.
5. **Llenar formulario** aprueba únicamente la copia de esos valores al navegador.
   Playwright verifica los controles observados, cierra la red antes del primer
   valor, rellena y confirma qué controles se completaron. No pulsa buscar,
   siguiente ni facturar. No se ha consultado o validado la compra en el comercio.

Si falta un identificador y existe una alternativa pública explícita —por ejemplo
«No cuento con Web ID»—, se abre esa opción y se observan sus campos. Solo se leen
de nuevo los datos que aún faltan. Una opción única se puede seguir después de una
lectura autorizada; ante varias opciones se elige en Telegram. No se convierten
Folio, Cuenta y Referencia entre sí. Véase el [sanity check completo](SANITY_FACTURACION.md).

El formulario se revisa de nuevo al aprobar el llenado. Si cambian su estructura,
valores, perfil o archivo, hace falta revisar la propuesta nueva. Las referencias
DOM se renuevan sin invalidar una propuesta semánticamente idéntica. Los botones
pertenecen a un usuario, ticket, documento y propuesta; no se pueden reutilizar
para otro usuario o repetir el llenado.

La sesión que contiene valores queda privada, sin red, hasta tres minutos. Las
herramientas públicas del modelo no pueden observarla, seguir enlaces o recibir
sus valores. Solo un estado nativo informa de completado o caducidad. Una respuesta
perdida se comprueba sin repetir el llenado. Esta entrega prepara campos en un
navegador efímero: aún no ofrece envío posterior ni una sesión remota interactiva
para el usuario. El envío al comercio y la recuperación de facturas quedan para
la siguiente fase y necesitan implementar sus operaciones y aprobación concreta.

## Archivos, reintentos y costes

Cada lectura real cuenta contra la misma cuota diaria de documentos, incluidos
fallos. Las lecturas consumen tokens; una reapertura del mismo formulario con
el mismo archivo reutiliza los datos y no repite la lectura. La navegación con
IA conserva su presupuesto separado de chat existente. No se envía el perfil a
la IA de extracción; solo el ticket y las etiquetas de los campos de compra.

Una lectura parcial no obliga a repetir todo: se pide el campo pendiente. No se
ofrece otra lectura de los mismos bytes y campos después de un intento. Una foto
nueva permite un intento nuevo, con autorización, y se compara con los datos
de identidad ya guardados. Si no coincide, se conserva el ticket anterior.
Una corrección humana nunca se sobrescribe con una lectura posterior.

Las autorizaciones de análisis anteriores no se amplían: **Leer datos que pide
el portal** autoriza expresamente la lectura dirigida del archivo que ya existe.
Las nuevas guardan el alcance versionado `portal-fields-v2`, que explica que puede
haber lecturas adicionales si una alternativa solicita datos nuevos. Los permisos
`portal-fields-v1` que prometían dos lecturas conservan ese límite: un formulario
nuevo pide autorización nativa antes de otra lectura. El correo de
facturación explícito sin portal mantiene su flujo: se leen los datos mínimos de
compra y se conserva la revisión y aprobación de correo existente.

Los nuevos tickets cifrados se retienen siete días como máximo, configurable con
`BILLING_TICKET_RETENTION_HOURS` (1–168; predeterminado 168). CSF y otros documentos
conservan `BILLING_DOCUMENT_RETENTION_HOURS` (24 horas por defecto). Volver a subir
los mismos bytes o reintentar una clasificación no renueva el plazo de un ticket
existente. Los metadatos y el perfil confirmado sobreviven al archivo temporal.
No se recuperan documentos ya vencidos ni se cambia el plazo de los antiguos.

Si la foto anterior venció, **Reenviar foto de este ticket** vincula el adjunto al
mismo ticket y perfil. Se solicita permiso para leerlo; no se pide otra CSF ni se
abre otra solicitud. No hace falta volver a subir una foto si todos los campos
necesarios ya están guardados: pueden completarse manualmente.

## Alcance y comprobación

Reconocer etiquetas y controles es genérico; no garantiza que funcione cualquier
sitio. CAPTCHA, login, campos desconocidos, controles no admitidos, formularios
dinámicos que requieren consultas de compra y opciones ambiguas requieren otra
fase o intervención. No se eleva el permiso público para intentar esas acciones.
El navegador conserva sus filtros de TLS, direcciones públicas y presupuestos.

Las pruebas usan Chromium y SDK reales con páginas/modelos ficticios: orden de
lectura, valores de Tienda, fechas, protección de correcciones, archivo vencido,
misma cuota, ausencia de reintentos, aislamiento por usuario, cambios en la
propuesta, llenado con eventos que intentan transmitir y estado tras respuesta
perdida. No hacen llamadas de IA de pago, solicitudes fiscales ni envíos reales.
La comprobación en Docker usa los mismos límites y usuario del despliegue.

Actualizar requiere reconstruir **agent y browser** con los dos Compose, usando
el bloque de [navegación](NAVEGACION.md#activar-desde-termius). Después, `/estado`
y **Revisar portal con IA**. El archivo vencido se reenvía únicamente con el botón
del ticket. Conservar `.env.hostinger`, clave de cifrado y volúmenes existentes.
