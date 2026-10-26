# Agente de IA en Telegram, con acceso por invitación

Telegram utiliza el mismo agente y herramientas que la web. Los mensajes libres
llegan al OpenAI Agents SDK; no necesitas una CSF para conversar, calcular o
consultar la hora. El flujo fiscal conserva botones de consentimiento y aprobación.
No se ha registrado automáticamente un bot real durante el desarrollo.

## Primera conexión desde el iPad y Termius

Necesitas el backend funcionando por HTTPS. El usuario ya comprobó el chat web
y un cálculo real con OpenAI; la conexión nueva de Telegram aún requiere pruebas.

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
- «Divide 500 pesos con 15 % de propina entre dos personas» usa OpenAI. Haz una
  primera petición únicamente cuando autorices su consumo de tokens.
- `/historial` recupera los últimos mensajes propios sin llamar al modelo.
- `/nuevo` inicia otra conversación; conserva las anteriores y los perfiles.
- `/conversaciones` permite volver a una anterior. `/eliminar_conversacion ID`
  solicita un botón de confirmación para borrar solo esa conversación propia.
- Una CSF/foto exige el botón para autorizar su análisis. El perfil se confirma
  después de elegir uso de CFDI y correo. Un mensaje no sustituye autorizaciones.
- «Prepara el correo de este ticket» solicita el borrador revisable. El botón
  muestra destinatario, contenido y adjuntos; solo su confirmación puede enviarlo.
  Gmail debe estar conectado y el envío habilitado por el operador.

Se comprobó con Runner real, modelo simulado y servicios ficticios. Esto no
sustituye la prueba desde tu cuenta. No se reintentan errores automáticamente.
Durante el alta fiscal puedes preguntar al agente sin elegir automáticamente el
uso de CFDI ni confirmar el perfil. Los comandos y botones siguen disponibles.

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

- Una instancia de agente; una ejecución general a la vez y una operación de
  Telegram a la vez por persona. Si está ocupado, avisa sin reenviar al modelo.
- Se reutilizan límites de pasos, tiempo, tamaño y consumo diario por persona.
  Todo el consumo usa la clave OpenAI del servidor. La reserva de tokens no es
  un límite exacto de dinero.
- Cada conversación y aprobación tiene propietario. Las identidades internas
  de Telegram no tienen contraseña ni acceso a la web.
- Datos fiscales y estado del canal cifrados. El historial general conserva
  SQLite privado, sin cifrado por mensaje. Usa adjuntos y controles fiscales para
  CSF/tickets; no pegues claves o documentos completos en la conversación libre.
  Telegram también conserva sus propias copias de mensajes y archivos.
- Web y Telegram tienen identidades independientes; los perfiles no se comparten
  automáticamente entre canales. WhatsApp no está implementado.
- Gmail, SAT y seguimiento siguen siendo activaciones separadas. Configura y
  prueba los respaldos privados antes de incorporar documentos reales o usuarios.
