# Mi agente · Asistente web privado

Un agente de IA con interfaz web en español para iPad y computadora. Usa **Python 3.12+, FastAPI y el OpenAI Agents SDK**, con una sola instancia de agente y dos herramientas autorizadas: cálculo y fecha/hora del servidor.

Repositorio del proyecto: [kevinriosb/prueba-Codex](https://github.com/kevinriosb/prueba-Codex).

**La aplicación todavía no está desplegada. El enlace de GitHub permite consultar el código; para usar el chat desde Safari hace falta una URL del backend.**

## Qué puedes hacer

- Conversar en lenguaje natural y hacer preguntas.
- Pedir: «Divide una cuenta de 500 pesos con 15 % de propina entre 2». El agente puede usar la herramienta de cálculo.
- Preguntar: «¿Qué fecha y hora tiene el servidor?». La herramienta responde con zona horaria explícita.
- Crear varias conversaciones y volver a ellas después de cerrar el navegador o reiniciar el servidor.
- Ver resultados de herramientas y errores en el chat.
- Aprobar o rechazar llamadas de futuras herramientas que modifiquen datos o sistemas externos.

La versión inicial tiene **un usuario propietario**, configurado por variables de entorno. No incluye registro público de usuarios. El backend vincula cada conversación y aprobación a su propietario; las pruebas comprueban que un usuario no puede leer ni modificar conversaciones ajenas.

No hay acceso a shell, ejecución de código generado, archivos privados, navegación web, correo ni análisis de imágenes en esta versión.

## Desde tu iPad: qué necesitas para probarla

Safari necesita una **URL HTTPS que sirva el backend**. Abrir `frontend/index.html` o una dirección `localhost` de este entorno no permite usar la aplicación desde tu iPad.

Tienes dos caminos:

1. **Sin contratar servicios:** ejecutar el proyecto en una computadora de tu red y acceder desde el iPad. Las instrucciones están abajo; esta modalidad es solo para desarrollo en una red de confianza.
2. **Posteriormente en Railway:** conectar el repositorio de GitHub y autorizar la creación/despliegue del servicio, configurar las variables y un volumen persistente. Railway y la API de OpenAI pueden generar cargos. No se ha creado ni contratado un servicio de Railway.

Cuando exista una URL accesible:

1. Ábrela en Safari e inicia sesión con `APP_LOGIN_USERNAME` y la contraseña que hayas elegido en `APP_LOGIN_PASSWORD`.
2. Pulsa **Hacer un cálculo**, luego el botón de enviar. El ejemplo debe dar **287.50 pesos por persona**.
3. Pulsa **Nueva conversación** y pregunta la fecha/hora.
4. Recarga Safari y comprueba que el historial sigue allí.
5. Si añades una herramienta de escritura en el futuro, revisa sus argumentos y confirma o rechaza en la tarjeta que aparece en el chat.

La clave de IA se configura en el servidor, **nunca en el chat ni en el navegador**.

## Ejecutarla localmente, paso a paso

Necesitas Python 3.12 o posterior y [uv](https://docs.astral.sh/uv/getting-started/installation/), un gestor que instala las dependencias fijadas. Abre una terminal en la carpeta de este repositorio:

```sh
uv sync --frozen
```

Duplica `.env.example` y llama **`.env`** a la copia. En una terminal de macOS/Linux puedes hacerlo con `cp .env.example .env`; en Windows, con `Copy-Item .env.example .env`. Este archivo está excluido de Git.

Edita al menos:

```dotenv
APP_LOGIN_USERNAME=admin
APP_LOGIN_PASSWORD=una-contraseña-propia-de-al-menos-12-caracteres
AI_API_KEY=tu-clave-real-de-la-api-de-openai
```

Los textos de ese ejemplo son marcadores: elige tu propia contraseña e introduce la clave únicamente en tu archivo privado. No los copies en un archivo público. La clave se obtiene en la cuenta de API de OpenAI; una suscripción de ChatGPT no la sustituye. Antes de usar la API, revisa su facturación y configura límites en el proveedor.

Inicia el servidor:

```sh
uv run --frozen python -m backend.app
```

En **esa misma computadora**, abre `http://localhost:8000`. Puedes verificar el estado en `/health`. Detén el proceso con `Ctrl+C`.

Sin `AI_API_KEY` puedes iniciar sesión y abrir el historial; al intentar enviar un mensaje verás un error de configuración y no se hará una llamada de IA. Sin una contraseña válida el servidor no inicia: no existe una contraseña predeterminada.

### iPad en la misma red local

En el `.env` de la computadora establece `HOST=0.0.0.0` y añade su IP local a `APP_ALLOWED_HOSTS`, por ejemplo `localhost,127.0.0.1,192.168.1.20`. Mantén `APP_ENV=development` y `APP_SECURE_COOKIES=false` para esta prueba HTTP. Reinicia el servidor y abre `http://192.168.1.20:8000` en Safari, usando la IP real de la computadora. Ambos dispositivos deben estar en la misma Wi-Fi.

HTTP no cifra el tráfico. Usa esta modalidad únicamente en una red de confianza y con credenciales de prueba, sin datos privados. Para acceso fuera de tu red, utiliza HTTPS y las opciones de producción. El entorno en la nube de esta conversación no es esa computadora: su `localhost` no es accesible desde tu iPad.

## Variables de entorno

| Variable | Uso | Valor inicial / requisito |
| --- | --- | --- |
| `APP_LOGIN_USERNAME` | Usuario propietario | `admin` |
| `APP_LOGIN_PASSWORD` | Contraseña del servidor | Obligatoria, 12–256 caracteres; el servidor conserva una comprobación Argon2id en SQLite |
| `AI_API_KEY` | Credencial de OpenAI, solo backend | Obligatoria para ejecutar IA; nunca se devuelve al navegador |
| `AI_MODEL` | Modelo compatible con Responses API y herramientas | `gpt-4.1-mini`; comprueba disponibilidad en tu cuenta |
| `APP_ENV` | Modo de ejecución | `development` o `production` |
| `APP_SECURE_COOKIES` | Cookie solo por HTTPS | Obligatorio `true` en producción |
| `APP_ALLOWED_HOSTS` | Dominios exactos que puede servir la aplicación | Lista separada por comas; sin `https://`, rutas o comodines |
| `APP_DATABASE_PATH` | Ruta del historial, sesiones y contadores | `data/agent.sqlite3`; en Railway `/data/agent.sqlite3` con volumen |
| `APP_SESSION_HOURS` | Duración de la sesión de acceso | `8` |
| `SERVER_TIMEZONE` | Zona de la herramienta de hora | `America/Mexico_City` |
| `MAX_INPUT_CHARS` | Longitud del mensaje | `4000` |
| `MAX_CONTEXT_BYTES` | Tamaño del historial enviado al modelo | `16000` |
| `MAX_OUTPUT_TOKENS` | Máximo de salida por llamada al modelo | `600` |
| `MAX_AGENT_TURNS` | Máximo de llamadas al modelo por ejecución del SDK | `3` |
| `MAX_TOOL_CALLS` | Máximo de herramientas ejecutadas por solicitud | `5` |
| `RUN_TIMEOUT_SECONDS` | Tiempo máximo de una ejecución | `35` segundos |
| `TOOL_TIMEOUT_SECONDS` | Tiempo de las herramientas incluidas | `3` segundos |
| `MAX_DAILY_RUNS` | Solicitudes diarias por propietario | `30`, incluidas reanudaciones y fallos iniciados |
| `MAX_DAILY_TOKENS` | Presupuesto local de admisión en tokens | `250000`; sujeto a la reserva explicada abajo |
| `MAX_CONVERSATION_TURNS` | Límite de conversación | `20`; después crea una nueva |
| `MAX_CONVERSATIONS` | Máximo de conversaciones guardadas | `30`; puedes eliminarlas con confirmación |
| `APPROVAL_TTL_SECONDS` | Caducidad de una confirmación | `900` segundos |
| `HOST`, `PORT` | Dirección y puerto de escucha | Local: `127.0.0.1:8000`; Railway establece `PORT` y Docker escucha `0.0.0.0` |

En el entorno de Codex usado para validar este proyecto, la clave funcionó desde la variable directa **`AI_API_KEY_CHECK`**. El vínculo `AI_API_KEY` de Secretos de red devolvió errores de autenticación; la causa no está determinada. Las instrucciones de inicio del entorno pasan la variable validada como `AI_API_KEY` únicamente al proceso del servidor. Esta excepción de desarrollo se documenta en [docs/VALIDACION.md](docs/VALIDACION.md); el código sigue leyendo `AI_API_KEY`.

`APP_LOGIN_PASSWORD` también necesita un valor real disponible para el proceso local: un marcador de secreto de red solo válido para peticiones HTTPS no sirve como contraseña local. **En Railway configura la clave directamente en la variable privada `AI_API_KEY`; no necesitas `AI_API_KEY_CHECK`.** No introduzcas credenciales en el chat ni en archivos versionados.

## Límites y control de consumo

El backend rechaza solicitudes concurrentes en lugar de formar una cola ilimitada. Aplica límites de tamaño, herramientas, pasos, tiempo y solicitudes diarias. El cliente de OpenAI tiene los reintentos automáticos desactivados.

Antes de iniciar una ejecución se reserva un número conservador de tokens calculado con los límites de contexto, salida y pasos. Si se conoce el uso final, se sustituye la reserva por el uso que reportó el SDK. Si hay un fallo o timeout, se conserva la reserva: el proveedor podría haber procesado una petición aunque no llegara su respuesta. Eliminar una conversación no restablece el consumo diario. Los contadores se reinician por día UTC y se conservan en SQLite.

La reserva es un control local de admisión, **no una garantía de un importe máximo en dólares**. Depende del modelo, de su tokenización y de sus tarifas. Un timeout del servidor tampoco garantiza que el proveedor deje de facturar una petición ya enviada. Configura además controles de gasto en OpenAI. Cambiar los límites puede hacer que una sola reserva supere el presupuesto diario; en ese caso el servidor rechazará la ejecución antes de llamar al modelo.

## Persistencia y seguridad

- Sesiones mediante cookies `HttpOnly`, `SameSite=Strict`, y `Secure` en producción. Solo se guarda el hash del token de sesión en la base de datos.
- Contraseña comprobada con Argon2id. Cambiarla y reiniciar invalida las sesiones existentes.
- Comprobación del origen, protección CSRF y validación del host en el servidor. No se habilita CORS abierto.
- Límite de cuerpo de 24 KB, incluso con cuerpos fragmentados, y 10 segundos para recibirlo.
- Consultas SQL parametrizadas y comprobación del propietario en cada conversación y aprobación.
- Salidas de la IA y argumentos de herramientas renderizados como texto, sin `innerHTML`.
- Calculadora con un intérprete de operaciones permitidas; no usa `eval`, shell ni ejecución de código.
- Trazas del SDK y telemetría nativa de FastAPI desactivadas; `store=False` en Responses. Los logs de auditoría solo contienen eventos, identificadores opacos, tiempos y contadores; no prompts, contraseñas, tokens ni argumentos de herramientas.
- La base de datos sí conserva el historial solicitado por el usuario y los estados pendientes. Está protegida por permisos de archivo, pero **no cifrada por la aplicación**: protege el disco, el volumen y sus copias. OpenAI procesa los mensajes conforme a las condiciones de su API; `store=False` no es una promesa de ausencia de toda retención del proveedor.
- No hay alta disponibilidad ni soporte para varias réplicas. Esta versión está diseñada para una réplica y un proceso Uvicorn. Antes de escalar, migra persistencia y coordinación a servicios compartidos.

## Confirmaciones para herramientas futuras

Las herramientas iniciales son de lectura y cálculo: no modifican sistemas externos. No hay un botón que publique, pague o envíe mensajes.

Para añadir una API, crea su módulo en `backend/app/tools/` y registra una `ToolSpec`. Si tiene efectos de escritura, usa `effect='write'`: el registro fuerza `needs_approval=True` en el SDK. Añade los permisos, validaciones, límites y pruebas necesarios antes de habilitarla. Nunca dejes que el modelo o el navegador elijan una URL, un comando o una herramienta arbitraria.

La ejecución se pausa con las interrupciones nativas del SDK. El estado completo queda solo en SQLite; el navegador recibe una vista de los argumentos y un identificador opaco. La decisión necesita autenticación, CSRF y pertenencia a la conversación. La aprobación es de un solo uso, caduca y se consume de forma atómica antes de reanudar. Se puede recuperar una aprobación pendiente después de reiniciar con la misma versión del agente.

Una confirmación no garantiza ejecución exactamente una vez en un sistema externo: si una herramienta hizo un cambio y la red falló, debes comprobarlo antes de repetir. Si falla una reanudación aprobada o el proceso se interrumpe durante una ejecución, la conversación queda bloqueada para evitar repeticiones automáticas. Crea una nueva solo después de revisar el resultado externo. Cada API futura deberá añadir sus propias claves de idempotencia y recuperación.

## Pruebas

```sh
uv run --frozen pytest -q
```

Las pruebas usan modelos simulados **con el Runner real del OpenAI Agents SDK**. Ejecutan las herramientas y el flujo nativo de aprobaciones, además de la API FastAPI. No necesitan clave de OpenAI ni hacen peticiones de pago. Cubren autenticación, CSRF, historial persistente, aislamiento entre propietarios, cálculo seguro, hora, límites, errores, logs y reanudación/aprobación/rechazo sin repetir efectos.

El resultado de las comprobaciones de este trabajo y sus límites se documenta en `docs/VALIDACION.md`.

### Prueba opcional de la interfaz

```sh
uv sync --frozen --group browser
uv run --group browser playwright install chromium
uv run --group browser python scripts/browser_smoke.py
```

Usa un servidor temporal limitado a la máquina local, una contraseña ficticia exclusiva de pruebas y un modelo simulado. Verifica login, chat, historial al recargar, confirmación/rechazo, HTML tratado como texto, errores y controles táctiles de iPad. No es una forma de iniciar producción. Si ya tienes Chromium instalado, puedes indicar su ruta con `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` sin descargar otro navegador.

### Prueba opcional del contenedor

```sh
docker build -t prueba-codex-agent:local .
uv run --frozen python scripts/container_smoke.py
```

La segunda orden crea y elimina únicamente sus contenedores y volumen locales de prueba. Comprueba salud, autenticación, permisos y persistencia al recrear el contenedor. No conecta OpenAI ni despliega en Railway.

Si tu red utiliza una autoridad de certificación privada, el Dockerfile permite proporcionar **su certificado de confianza** mediante el secreto de BuildKit `build_ca`. No desactives TLS ni alteres los hashes. En este entorno se utilizó `docker build --secret id=build_ca,src=/etc/ssl/certs/ca-certificates.crt -t prueba-codex-agent:local .`. El certificado se monta solo durante la instalación y no se copia a la imagen final.

## Railway: preparación para un despliegue posterior

**El despliegue en Railway requiere autorización y revisión de costos. No se ha ejecutado.**

1. Revisa el código en GitHub, en la rama `main` de `kevinriosb/prueba-Codex`. El repositorio tiene una configuración de pruebas de CI; no contiene un workflow de despliegue automático.
2. En Railway crea un servicio desde ese repositorio y usa el `Dockerfile`. `railway.json` define el arranque, una réplica, `/health` y reinicios limitados.
3. Configura `APP_LOGIN_USERNAME`, `APP_LOGIN_PASSWORD`, `AI_API_KEY`, `AI_MODEL`, `APP_ENV=production`, `APP_SECURE_COOKIES=true` y `APP_DATABASE_PATH=/data/agent.sqlite3`.
4. Genera un dominio HTTPS. En `APP_ALLOWED_HOSTS` añade el dominio exacto que te dé Railway y `healthcheck.railway.app,localhost,127.0.0.1`. El servicio debe permanecer detrás del HTTPS de Railway. No necesitas habilitar CORS ni confiar en cualquier encabezado de proxy.
5. Añade un **volumen persistente montado en `/data`** y verifica que permita escribir al usuario del contenedor (UID 10001). Un volumen es necesario para conservar conversaciones, sesiones y contadores tras una recreación. Sin volumen, el disco del contenedor puede perderse. Configura copias y revisa costos antes de crear recursos.
6. Despliega una sola réplica. Comprueba que `/health` devuelve `{"status":"ok","version":"0.1.0"}` y prueba el login, un cálculo y la recuperación del historial después de reiniciar.
7. Abre esa URL HTTPS desde Safari en el iPad. Opcionalmente, usa **Compartir → Añadir a pantalla de inicio**.

El contenedor se ejecuta sin usuario root y no incluye `.env`, pruebas ni credenciales. No debes aumentar workers o réplicas manteniendo esta SQLite y los límites en memoria.

## Organización del código

```text
backend/app/
  main.py             API, autenticación de rutas y cabeceras
  config.py           variables y validación
  auth.py             contraseña, sesión y CSRF
  database.py         historial, presupuesto y decisiones atómicas
  agent_service.py    un agente, Runner, límites y aprobaciones
  audit.py            registro seguro de metadatos
  security.py         límite de tamaño y recepción de solicitudes
  tools/              calculadora, reloj y registro de permisos
frontend/             HTML, CSS y JavaScript sin frameworks
tests/                pruebas sin peticiones de pago
uv.lock               dependencias exactas y hashes
requirements.txt      exportación de producción con hashes para Docker
Dockerfile            arranque de producción
railway.json          configuración para Railway, sin despliegue ejecutado
```

Si cambias dependencias, actualiza `uv.lock` con `uv lock` y regenera la exportación de producción con `uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt`. No modifiques hashes para saltarte verificaciones.

El flujo de ejecución y la forma de añadir herramientas están explicados en [docs/ARQUITECTURA.md](docs/ARQUITECTURA.md).
