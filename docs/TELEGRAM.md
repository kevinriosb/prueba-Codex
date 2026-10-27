# Agente de IA en Telegram, con acceso por invitación

Telegram utiliza el mismo agente y herramientas que la web. Los mensajes libres
llegan al OpenAI Agents SDK; no necesitas una CSF para conversar, calcular o
consultar la hora. El flujo fiscal conserva botones de consentimiento y aprobación.
No se ha registrado automáticamente un bot real durante el desarrollo.

## Primera conexión desde el iPad y Termius

Necesitas el backend funcionando por HTTPS. El usuario ya comprobó el chat web,
un cálculo real con OpenAI y la conversación en su bot nuevo. Estos pasos sirven
para una primera instalación; las actualizaciones no exigen repetir la vinculación.

1. En Telegram abre [@BotFather](https://t.me/BotFather), envía `/newbot`, elige
   nombre y usuario terminado en `bot`. Guarda el token completo en privado.
2. Desde Termius, conectado al VPS, ejecuta:

   ```sh
   cd /opt/lilyapp-agent
   git pull --ff-only
   python3 scripts/configure_hostinger_telegram.py
   ```

3. Pega el token cuando lo solicite: la entrada está oculta. El asistente verifica
   el bot y muestra su `@usuario`. Escribe `SI` solo si es el bot nuevo que creaste.
4. Abre el enlace del bot, pulsa **Iniciar** y envíale el código `/vincular_...`
   mostrado en Termius. Todavía no responderá. Vuelve a Termius y pulsa Enter.
   No envíes documentos en esta etapa ni compartas el código de vinculación.

La vinculación exige el mensaje exacto desde una persona en un chat privado, con
el mismo ID de remitente y chat. El código aleatorio caduca a los diez minutos.
No autoriza a la primera cuenta que escriba: debe presentar ese código. Si aparece
en varias cuentas, no autoriza ninguna. Hace hasta tres consultas al pulsar Enter.
Un webhook existente bloquea la vinculación; no se borra ni sustituye.

Solo guarda las tres variables de Telegram, con copia privada anterior. No cambia
OpenAI, contraseña, clave de cifrado, datos o correo. Rechaza una configuración de
Telegram existente para no sobrescribirla. Solo consulta `getMe`, `getWebhookInfo`
y `getUpdates`: no hay IA, envío de mensajes desde el script o registro de webhook.

Cuando indique que tu cuenta está verificada, actualiza **solo agent**:

```sh
docker compose -f compose.hostinger.yaml build agent
docker compose -f compose.hostinger.yaml up -d --no-deps --no-build --force-recreate --wait --wait-timeout 120 agent
curl --fail --silent --show-error https://agente.lilyapp.mx/health
```

Se conserva el volumen y la clave de cifrado. No se detiene n8n ni otros proyectos.
La reconstrucción incorpora el código nuevo; no solicita IA.

Valida primero y registra después el webhook:

```sh
docker compose -f compose.hostinger.yaml exec -T agent python - --url https://agente.lilyapp.mx < scripts/configure_telegram.py
docker compose -f compose.hostinger.yaml exec -T agent python - --url https://agente.lilyapp.mx --apply < scripts/configure_telegram.py
```

El primer comando no usa APIs. `--apply` autoriza registrar el webhook en Telegram;
rechaza sobrescribir otra dirección y consulta de nuevo para verificar la URL.
No envía correos ni solicita IA. Los scripts se ejecutan por stdin dentro del
contenedor: no necesitas instalar el SDK en el Python 3.9 del sistema.

## Qué probar

- `/start` muestra ayuda sin solicitar generación.
- `/perfil` retoma el siguiente paso del alta **dentro de Telegram**. Elegir el
  uso de CFDI muestra botones; después escribes el correo y confirmas la revisión
  completa. «Tienes los datos para facturar?» abre este mismo flujo sin llamar al
  modelo ni terminar con «¿te ayudo?». «Sí» abre el paso pendiente, nunca confirma.
- Con perfil guardado, `/perfil` → **Ver o editar perfil** permite cambiar correo
  o uso de CFDI. También se acepta `mi correo es nuevo@example.com`. Los cambios
  quedan en borrador hasta pulsar **Confirmar y guardar**. **Actualizar CSF**
  permite revisar nuevos datos fiscales y reemplazar el mismo perfil, conservando
  sus tickets. Requiere consentimiento de análisis y confirmación de la revisión.
  No cambies datos fiscales escribiéndolos al modelo: proceden de la constancia.
- «Divide 500 pesos con 15 % de propina entre dos personas» usa OpenAI. Haz una
  primera petición únicamente cuando autorices su consumo de tokens.
- `/historial` recupera los últimos mensajes propios sin llamar al modelo.
- `/nuevo` inicia otra conversación; conserva las anteriores y los perfiles.
- `/conversaciones` permite volver a una anterior. `/eliminar_conversacion ID`
  solicita un botón de confirmación para borrar solo esa conversación propia.
- Antes del primer perfil, un archivo desconocido pide elegir **CSF o Ticket**.
  Una vez recibida la CSF o confirmado el perfil, los archivos nuevos se tratan
  como tickets. No se pide elegir el tipo en cada subida. Para otra constancia,
  usa `/csf` como descripción; `/ticket` también está disponible.
- Si envías primero el ticket, se guarda cifrado y queda pendiente hasta confirmar
  el perfil fiscal. Después de guardar el perfil aparece **Continuar con ticket**:
  utiliza el mismo archivo, sin volver a subirlo, y solicita autorizar su análisis.
- `/documentos` recupera archivos propios vigentes y permite corregir el tipo.
  También puedes escribir **«Es un ticket»** para corregir el último archivo.
  Los archivos conservan su vencimiento original; clasificarlos no amplía el TTL.
- **Repetir solicitud** y `/estado` abren el ticket guardado, su estado y el paso
  pendiente. No repiten análisis, envíos ni el alta fiscal. `/tickets` permite
  elegir entre varias compras; **Actualizar estado** vuelve a consultar el registro.
  **Datos listos** no significa solicitado: el bot indica si no hubo envío,
  falta confirmar el borrador, se espera respuesta o existe un envío incierto.
  **Consultar respuestas** consulta Gmail únicamente al pulsarlo, si ya hubo envío.
- Una foto idéntica de un ticket vigente y del mismo perfil reutiliza su análisis
  sin consumo adicional de IA ni otro archivo. Otra foto de la misma compra puede
  necesitar una lectura autorizada para reconocerla; después abre la solicitud
  original y conserva la copia cifrada hasta su vencimiento. No duplica el envío.
- `/limites` muestra los contadores propios y la hora de restablecimiento en la
  zona horaria del servidor. Por defecto hay **10 análisis de archivos**, **30
  ejecuciones de chat IA** y **100 mensajes/acciones** diarios, con presupuestos
  separados. Los análisis de CSF y tickets comparten el primer contador; los
  intentos fallidos también cuentan. Actualizar el contenedor no reinicia cuotas.
  Los límites diarios se renuevan a las 00:00 UTC, mostradas en hora local.
  Un ticket bloqueado por la cuota conserva el archivo y el aviso anterior como
  historial. `/estado` consulta la cuota vigente: con cupo muestra **Pendiente de
  análisis** y permite autorizar el mismo archivo; sin cupo muestra **En espera de
  cupo**, su restablecimiento y oculta el botón de análisis. Consultar el estado
  no consume un análisis ni borra intentos anteriores. Si el archivo ya venció,
  pide otra copia del ticket y conserva el perfil fiscal.
  La reserva de tokens, el espacio de archivos y los turnos por conversación son
  límites distintos: el bot identifica cuál impidió la operación.
- `/estado`, `/tickets`, `/limites`, **Actualizar estado** y las frases de consulta
  reconocidas no generan IA ni consumen los 100 mensajes/acciones. Tienen una cota
  independiente de 500 consultas al día por usuario. Así puedes revisar una compra
  aunque hayas agotado el chat o los análisis. El límite del proveedor OpenAI es
  independiente de estos contadores locales. No se elevan cuotas automáticamente.
- La CSF se analiza con autorización; el perfil se confirma después de elegir uso
  de CFDI y correo. Mencionar esos datos en el chat no crea un perfil confirmado.
  Si la CSF ya llegó, el bot pide solo el paso pendiente, sin solicitarla otra vez.
  El perfil confirmado se conserva al usar `/nuevo`, reiniciar o vencer el archivo
  temporal de la CSF. Actualizar datos o adjuntar una CSF original vencida puede
  requerir una nueva copia, según el caso.
- Si falta extraer un dato, el bot indica **RFC, nombre, CP o régimen pendiente**,
  sin declarar ilegible todo el archivo. Puedes completar el campo desde tu CSF
  en este chat; para el nombre usa `/dato NOMBRE`, para el régimen hay botones.
  Revisa todos los datos antes de guardar. **Usar otra CSF** permite reemplazar
  el archivo del alta pendiente sin tratarlo como ticket ni repetir el error viejo.
  **Usar último PDF como CSF** recupera un archivo guardado que haya quedado
  clasificado incorrectamente, sin descargarlo ni subirlo otra vez.
  El PDF puede ser digital o escaneado: no se exige convertirlo ni capa de texto.
  Un régimen por nombre se convierte solo por equivalencia exacta del catálogo;
  los regímenes desconocidos o ambiguos requieren elección humana.
- «Prepara el correo de este ticket» solicita el borrador revisable. El botón
  muestra destinatario, contenido y adjuntos; solo su confirmación puede enviarlo.
  Gmail debe estar conectado y el envío habilitado por el operador.
- El ticket conserva los campos válidos aunque falte fecha, folio u otro dato.
  **Completar dato** permite escribirlo con `/dato_ticket VALOR`, sin otra lectura
  de IA. La página impresa admite direcciones sin `https://`; los QR de fotografías
  se decodifican localmente sin visitar sus enlaces. Un QR de encuesta no se toma
  automáticamente como portal de facturación.
- Con página identificada aparece **Ver datos para el portal**: muestra el enlace,
  la compra y tu perfil. La captura en el portal sigue siendo manual; no hay un
  adaptador comercial que emita o solicite automáticamente esa factura.
- Cuenta, Folio, Referencia y Código de facturación se extraen y guardan por
  separado. Antes de mostrar la propuesta, el agente intenta leer los campos del
  formulario público de una página aprobada. Para Al Bat está aprobada
  `https://f.zetus.mx/albat`. Esa consulta no envía datos del ticket, RFC o CSF.
  Si el portal pide **Cuenta**, muestra Cuenta; no la rellena silenciosamente con
  el folio. Si falta el valor, permite completarlo o autorizar una lectura nueva
  del archivo anterior a esta versión, sin volver a subirlo.
- Cuando no logra leer el formulario, distingue la plantilla observada en la
  captura de una consulta en vivo y muestra folio/referencias como alternativas
  **sin validar**. Leer los campos del formulario no comprueba la cuenta contra
  la base del comercio, no pulsa **Siguiente** y no genera una factura.
  Otros portales deben registrarse como páginas públicas aprobadas antes de poder
  consultarlos mediante `BILLING_PORTAL_INSPECTION_URLS`.
- Si no hay página y sí un correo explícito de facturación, **Confirmar correo +
  revisar con CSF** prepara una solicitud con ticket, perfil y CSF completa por tu
  elección. Revisa el destinatario en el original y después el borrador completo.
  Solo **Confirmar y enviar** puede enviarlo. Gmail debe estar conectado y el envío
  habilitado; de lo contrario se presenta como vista previa sin botón de envío.
- Cada persona utiliza `/conectar_gmail` para elegir su propio Gmail. Hay un solo
  cliente OAuth de la aplicación en el servidor, con tokens cifrados separados por
  usuario; no se configura una cuenta de correo global para todos los invitados.
  El correo receptor del perfil y el buzón emisor conectado pueden ser distintos.
- Si una lectura falló antes de esta actualización, `/tickets` ofrece **Autorizar
  nueva lectura guardada**. Usa los mismos bytes y vencimiento, conserva el intento
  anterior y solo admite un nuevo intento por versión. Requiere autorizar el
  consumo de OpenAI; no se reintenta automáticamente ni se reenvían correos.
- «Por qué no es fiable?» muestra el diagnóstico guardado del archivo actual:
  distingue formato, autenticación, cuota o tiempo de espera. No inventa una causa
  sobre la CSF cuando el archivo es un ticket y no solicita otra generación.

Se comprobó con Runner real, modelo simulado y servicios ficticios. Esto no
sustituye la prueba desde tu cuenta. No se reintentan errores automáticamente.
Durante el alta fiscal puedes preguntar al agente sin elegir automáticamente el
uso de CFDI ni confirmar el perfil. Los comandos y botones siguen disponibles.

Si un ticket anterior fue tratado como CSF, actualiza el backend y usa
`/documentos` → archivo → **Es un ticket**. El intento anterior se conserva;
una extracción con otro esquema requiere una nueva autorización de análisis.
No se reutilizan los campos extraídos como CSF para inventar los de un ticket.

## Invitar y retirar personas

Esta versión usa **autorización manual por ID**, sin registro público ni enlaces
que den acceso a cualquiera. El operador administra la lista desde Termius; el
agente no puede añadir personas por un mensaje, herramienta o documento.

Pide al invitado su ID numérico y verifica que corresponde a esa persona. El
`@usuario` no lo sustituye. Una forma habitual de consultar el ID propio es
`@userinfobot`, un servicio de terceros: no le envíes documentos ni credenciales.
No lo necesitas para la primera cuenta, que se verifica por código directamente.

Para autorizar a alguien:

```sh
python3 scripts/manage_telegram_users.py add
docker compose -f compose.hostinger.yaml up -d --no-deps --no-build --force-recreate --wait --wait-timeout 120 agent
```

Introduce el ID cuando lo pida y después comparte el enlace de tu bot con esa
persona. Las cuentas fuera de la lista y los grupos no provocan llamadas de IA
ni reciben datos privados. Antes de usar datos reales, prueba con dos cuentas
que cada una solo ve sus propios perfiles, tickets e historial.

Para retirar una cuenta, utiliza `remove` en lugar de `add` y recrea el servicio.
Sus datos se conservan; retirar acceso no equivale a borrarlos. No se permite
retirar la última cuenta desde este asistente. Cambiar la lista no exige volver
a registrar el webhook ni cambia su secreto. La lista admite hasta 100 cuentas;
no es una garantía de capacidad para atenderlas simultáneamente.

## Límites y privacidad

- Telegram pide una frase directa con objetivo de 50 caracteres y tope de
  `TELEGRAM_REPLY_MAX_CHARS=160`. Los pasos simples del perfil no superan 50.
  Las revisiones fiscales, consentimientos y correos muestran los datos completos:
  no se recortan para aprobar. El texto del modelo elimina ofertas genéricas
  finales y aplica el tope de presentación sin otra llamada al modelo.
- El máximo de salida enviado al proveedor para Telegram es
  `min(100, MAX_OUTPUT_TOKENS)` por llamada. La web conserva el valor predeterminado
  de 150. El recorte de presentación no recupera tokens ya generados. La selección
  de uso, captura de correo y confirmación se resuelven sin generar texto de IA.
- Instrucciones, herramientas e historial también consumen tokens de entrada.
  `/nuevo` reduce el contexto de futuras peticiones y conserva perfiles y tickets.
  La extracción estructurada de documentos mantiene su presupuesto independiente,
  para no truncar los campos fiscales. Las revisiones de confirmación muestran
  todos los datos necesarios y no solicitan generación de texto por sí solas.
- Las fotos de documentos se envían con detalle alto para leer letra pequeña;
  esto puede aumentar tokens de entrada respecto al detalle bajo anterior. No hay
  reintentos de análisis automáticos: exige consentimiento y conserva el resultado.
  Completar un campo manualmente no vuelve a llamar al modelo.
- Una instancia de agente; una ejecución general a la vez y una operación de
  Telegram a la vez por persona. Si está ocupado, avisa sin reenviar al modelo.
- Se reutilizan límites de pasos, tiempo, tamaño y consumo diario por persona.
  Todo el consumo usa la clave OpenAI del servidor. La reserva de tokens no es
  un límite exacto de dinero.
- Cada conversación y aprobación tiene propietario. Las identidades internas
  de Telegram no tienen contraseña ni acceso a la web.
- La revisión del perfil queda vinculada al borrador y a su versión por 15 minutos
  como máximo. Editar datos invalida botones anteriores; cada confirmación solo
  puede consumirse una vez. Los botones antiguos sin versión abren una revisión
  nueva y no guardan automáticamente el perfil.
- Datos fiscales y estado del canal cifrados. El historial general conserva
  SQLite privado, sin cifrado por mensaje. Usa adjuntos y controles fiscales para
  CSF/tickets; no pegues claves o documentos completos en la conversación libre.
  Telegram también conserva sus propias copias de mensajes y archivos.
- Web y Telegram tienen identidades independientes; los perfiles no se comparten
  automáticamente entre canales. WhatsApp no está implementado.
- Gmail, SAT y seguimiento siguen siendo activaciones separadas. Configura y
  prueba los respaldos privados antes de incorporar documentos reales o usuarios.
