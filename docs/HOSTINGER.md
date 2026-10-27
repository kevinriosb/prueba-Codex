# VPS Hostinger con AlmaLinux y CyberPanel

La navegación pública adaptable es opcional y añade un contenedor sin acceso a
claves ni documentos. Su activación y las actualizaciones posteriores usan también
`compose.browser.yaml`: consulta [Navegación adaptable](NAVEGACION.md). El despliegue
de esta guía con un solo Compose conserva su comportamiento anterior.

Preparación para una instancia privada del agente. No instala otro servidor web
ni se conecta automáticamente a ningún VPS. Los cambios del proxy requieren
ejecutar explícitamente el asistente con `--apply`. Crear archivos de despliegue
en GitHub no publica una aplicación.

El servidor revisado por el usuario tiene AlmaLinux 9, dos núcleos, unos 8 GB de
RAM, Docker 29.4.0, Compose 5.1.2 y CyberPanel/LiteSpeed en los puertos 80/443.
SELinux está desactivado en ese servidor; **esta instalación no requiere
desactivarlo ni cambia su estado**. Si se habilita posteriormente, revisa la
política de red del proxy según las denegaciones registradas.

La propuesta es un subdominio `agente.lilyapp.mx`, conservando la página existente.
Revisa los registros y servicios reales antes de aplicar cambios.

## 1. Revisar antes de instalar

Desde Termius, conectado al VPS, consulta:

```sh
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
ss -lnt '( sport = :18000 )'
command -v git
python3 --version
```

El puerto local 18000 debe estar disponible. Si existe un proyecto anterior con
el nombre `lilyapp-agent`, revisa su configuración; no lo sustituyas. Comprueba
también que `/opt/lilyapp-agent` esté libre. No detengas los contenedores ni la web
actual para realizar estas consultas.

Antes de cambios de producción, conserva una copia recuperable de la
configuración de CyberPanel y de la web. Un respaldo nuevo no se considera
comprobado hasta probar su restauración en un entorno separado.

## 2. Obtener el proyecto

Cuando se haya autorizado la instalación, desde una cuenta administradora del
VPS:

```sh
umask 077
git clone https://github.com/kevinriosb/prueba-Codex.git /opt/lilyapp-agent
cd /opt/lilyapp-agent
chmod 700 /opt/lilyapp-agent
python3 scripts/configure_hostinger.py
```

Si falta Git, su instalación en AlmaLinux es `dnf install git`, revisando la
operación que propone el gestor. El asistente necesita Python 3.9+ del sistema;
el agente usa Python 3.12 dentro de Docker. No reemplaces el Python del sistema.

Si el repositorio es privado y Git solicita autenticación, prepara un acceso de
lectura por clave de despliegue o el método autorizado de GitHub. No incluyas un
token en la URL del repositorio, en un comando ni en este chat.

El asistente pide dominio, usuario, contraseña nueva y clave de OpenAI opcional.
Las entradas privadas no se muestran. Genera una clave de cifrado aleatoria de
32 bytes y crea `.env.hostinger` con permisos `600`. **Nunca sobrescribe un archivo
o enlace existente.** No despliega, no registra Telegram y no hace llamadas a
OpenAI o Gmail. No lo ejecutes mediante una tubería.

Guarda una copia segura de `.env.hostinger` fuera del VPS antes de usar datos
reales. Contiene la clave de cifrado necesaria para recuperar perfiles,
documentos y tokens de Gmail. Perderla impide recuperar esos datos aunque
conserves SQLite. Para editar credenciales utiliza un editor en la terminal o
SFTP; no escribas secretos como argumentos de comandos. No envíes capturas del
archivo.

El archivo está excluido de Git y del contexto de construcción. Compose 2.30+
lee sus valores literalmente, incluidos `$`, `#`, espacios y comillas. Escribe
`CLAVE=valor`, **sin comillas envolventes** y en una sola línea. No ejecutes
`source .env.hostinger`: no es un script de shell.

## 3. Construir y probar solo en el VPS

```sh
docker compose -f compose.hostinger.yaml config --quiet
docker compose -f compose.hostinger.yaml build
docker compose -f compose.hostinger.yaml up -d --wait --wait-timeout 120
curl --fail --silent --show-error http://127.0.0.1:18000/health
docker compose -f compose.hostinger.yaml ps
```

Se espera `{"status":"ok","version":"0.1.0"}`. Todavía no se puede abrir
esa dirección local desde el iPad. La configuración solo publica
`127.0.0.1:18000`; no abre 18000 a internet ni ocupa 80/443. No hace falta añadir
una regla de firewall para 18000. Los envíos de correo, consultas SAT y
seguimiento automático permanecen desactivados inicialmente.

El contenedor usa un usuario sin privilegios, raíz de solo lectura, directorio
temporal acotado, sin capacidades Linux, una CPU y hasta 1 GB de RAM. No se
monta el socket Docker. El volumen `lilyapp-agent_agent_data` contiene SQLite en
`/data/agent.sqlite3`; no cambies el nombre del proyecto o volumen entre
actualizaciones. Los logs se rotan con un límite de tres archivos de 10 MB.

`restart: unless-stopped` vuelve a iniciar el servicio tras fallos o reinicios
del servidor mientras Docker esté activo. Un estado `unhealthy` por sí solo no
provoca un reinicio automático: configura alertas externas y revisa los fallos.

No ejecutes `docker compose config` sin `--quiet` para compartir resultados:
su salida puede contener las credenciales resueltas. No uses `down --volumes`
en producción, pues elimina el almacenamiento.

## 4. Dominio, certificado y proxy de CyberPanel

Esta fase debe adaptarse a la configuración actual del panel. No reemplaces
los listeners globales ni el virtual host de `lilyapp.mx` con una plantilla.

1. En el proveedor que administra el DNS autoritativo, crea un registro A para
   `agente` dirigido a la IPv4 del VPS. Conserva los registros de la web, correo y
   otros servicios. Si ya existe un registro `agente`, revísalo antes de cambiarlo;
   revisa también un posible AAAA para no dirigir clientes a otro servidor.
2. Crea un sitio o subdominio independiente para `agente.lilyapp.mx` en
   CyberPanel, comprueba que escucha en HTTP/HTTPS y emite su certificado.
3. Revisa el virtual host de **ese subdominio**. El asistente espera el archivo
   normal `/usr/local/lsws/conf/vhosts/agente.lilyapp.mx/vhost.conf`, con los
   bloques ACME, TLS y accesslog del sitio recién creado. Rechaza un contexto raíz
   o proxy existente para no reemplazar otra aplicación.
4. Desde `/opt/lilyapp-agent`, prueba primero:

   ```sh
   python3 scripts/configure_cyberpanel_proxy.py --domain agente.lilyapp.mx
   ```

   Este comando consulta el archivo, valida la configuración actual de LiteSpeed
   y comprueba el backend local; no modifica el sitio. Cuando esté autorizado el
   cambio, aplica con:

   ```sh
   python3 scripts/configure_cyberpanel_proxy.py --domain agente.lilyapp.mx --apply
   ```

   OpenLiteSpeed puede devolver código 1 cuando solo emite avisos. El asistente
   reconoce ese resultado únicamente si todas sus líneas son registros válidos
   sin errores. Un código 2, mensajes ERROR/FATAL, fallos no reconocidos o una
   recarga con código no cero siguen bloqueando el cambio. No uses una opción de
   forzado ni borres sitios para eludir la validación. Si un dominio antiguo ya
   no se utiliza, revisa sus bloques y mappings completos antes de desactivar sus
   referencias, con copia de seguridad; los archivos y contenedores se conservan.

   Hace una copia privada del archivo e instala un proxy a `127.0.0.1:18000`,
   conservando el certificado y el contexto ACME. Redirige las demás solicitudes
   HTTP a HTTPS, desactiva caché para la aplicación y excluye query strings y
   cabeceras del accesslog para no registrar códigos OAuth o cookies. El tiempo
   de conexión es de 60 segundos; conserva los límites del backend.
5. El asistente valida la configuración propuesta antes de recargar LiteSpeed
   y comprueba `/health` por HTTPS. Si falla, intenta restaurar el archivo y la
   configuración activa anterior; informa si no consigue confirmar la recarga
   de retorno. La recarga ordenada corresponde al servidor web compartido, pero
   el único archivo editado es el de este subdominio. Conserva la copia indicada
   incluso tras un resultado satisfactorio. No toca Docker, n8n, DNS, firewall,
   SELinux ni las credenciales de la aplicación.
6. Comprueba en Safari `https://agente.lilyapp.mx/health`, el certificado válido,
   login y logout. Un error de origen exige revisar `Host` y `Origin`; no lo
   resuelvas abriendo CORS, quitando CSRF ni desactivando las cookies seguras.

La aplicación mantiene `APP_ENV=production` y cookies `Secure`. Solo admite el
dominio configurado y los hosts locales de salud. No confía en encabezados de
proxy procedentes de cualquier dirección: si hace falta reconocer la IP del
cliente, configura únicamente el proxy concreto en `FORWARDED_ALLOW_IPS`, tras
verificar cómo cruza la red de Docker. No utilices `*`.

### Retirar referencias de un dominio que ya no se utiliza

Un dominio expirado puede seguir registrado en el archivo de LiteSpeed y provocar
errores al validar o recargar. Si su propietario confirmó que ya no lo utiliza,
revisa antes sus bloques completos y los listeners compartidos. El asistente de
retirada conserva todos los archivos, certificados, bases de datos y contenedores;
desactiva únicamente definiciones y mappings del dominio indicado.

Desde la carpeta del proyecto, sustituye `DOMINIO_ANTIGUO` por el dominio revisado:

```sh
python3 scripts/retire_cyberpanel_domain.py --retire-domain DOMINIO_ANTIGUO --keep-hosts lilyapp.mx,inicio.lilyapp.mx,agente.lilyapp.mx
```

Por defecto solo muestra el plan. Rechaza mappings mezclados con sitios activos,
estructuras no reconocidas o cambios en las rutas protegidas. Si un listener
compartido usa como respaldo un certificado del dominio retirado, exige un único
par de archivos ya usado por otro listener que atienda los sitios protegidos y
reutiliza esas rutas. No solicita certificados nuevos ni elimina los anteriores.

Cuando se haya revisado y autorizado el plan:

```sh
python3 scripts/retire_cyberpanel_domain.py --retire-domain DOMINIO_ANTIGUO --keep-hosts lilyapp.mx,inicio.lilyapp.mx,agente.lilyapp.mx --apply
python3 scripts/configure_cyberpanel_proxy.py --domain agente.lilyapp.mx --apply
```

El primer comando crea una copia privada del archivo global y valida la propuesta
después de prepararla. Si falla, restaura el archivo sin recargar el servidor:
no intenta recargar la configuración antigua, que puede contener errores. Si pasa,
la configuración queda preparada en disco. El segundo comando instala por primera
vez el proxy y hace la recarga ordenada, validando antes y comprobando salud HTTPS
después. Si el proxy ya estaba instalado, su operación idempotente no fuerza una
recarga: en mantenimiento hay que coordinar esa recarga pendiente por separado.

El acceso mediante los dominios retirados se desactiva tras la recarga; los
contenedores como n8n continúan y sus datos se conservan. Los registros históricos
del panel permanecen: una reconstrucción desde CyberPanel puede volver a generar
referencias. Revisa la configuración después de cambios del panel. No utilices
**Delete Website** como sustituto de este procedimiento, porque puede borrar datos.

### Si la página abre pero la IA devuelve un error

`/health` comprueba que el proceso está disponible; no hace una llamada a OpenAI
ni valida la clave, los permisos, el modelo o el saldo. Desde la carpeta del
proyecto puedes comprobar la conexión usando las variables efectivas del
contenedor, sin mostrar sus valores:

```sh
git pull --ff-only
docker compose -f compose.hostinger.yaml exec -T agent python - < scripts/check_openai.py
```

El archivo se ejecuta por la entrada estándar: no hay que copiar scripts a la
imagen ni reconstruir o reiniciar la aplicación. No lee ni modifica SQLite,
documentos, `.env.hostinger` o certificados. No solicita generación de IA:

- Primero envía `{}` a Responses **sin credencial** y exige HTTP 401. Si recibe
  otro resultado, se detiene sin enviar la clave. Un listado público de modelos
  no demuestra que una credencial sea válida.
- Repite el cuerpo vacío con la clave del contenedor. HTTP 400 con
  `missing_required_parameter` para `model`, después del control anterior,
  permite comprobar la autenticación de esa solicitud sin modelo ni entrada.
- Solo después consulta los metadatos del modelo configurado. Una clave con
  permisos restringidos puede permitir Responses sin permitir esta consulta;
  HTTP 403 al consultar metadatos no prueba que generar esté bloqueado.

El diagnóstico usa exclusivamente `https://api.openai.com/v1`, verifica TLS y
rechaza redirecciones. Muestra categorías conocidas y códigos HTTP; no muestra
cabeceras, cuerpos de error, mensajes internos ni valores de variables. No
reintenta solicitudes. El código de salida es 0 si pasan las comprobaciones y 1
si no se pueden completar. Puedes compartir únicamente esa salida.

Un resultado favorable **no comprueba el saldo, los permisos de generación ni
una respuesta real**. Si la prueba pasa y el chat sigue fallando, hace falta
diagnosticar esa ejecución con metadatos seguros antes de seguir probando.
No repitas peticiones de pago automáticamente. Una nueva prueba con generación
requiere el cupo autorizado para esa prueba.

Si el resultado es **HTTP 401, credencial rechazada**, sustituye la clave que usa
el contenedor. Necesitas la clave completa de OpenAI que empieza por `sk-`, no la
contraseña del VPS ni la del agente. Si ya no tienes el valor completo, crea otra
en [API Keys de OpenAI](https://platform.openai.com/api-keys), dentro del proyecto
que quieras usar. No compartas la clave por el chat, capturas o comandos.

Ejecuta directamente en la terminal interactiva del VPS:

```sh
python3 scripts/update_openai_key.py
```

El asistente pide pegar la clave con entrada oculta. Enter sin texto cancela con
código 1. Rechaza comillas, espacios, prefijos y configuraciones ambiguas; el
formato correcto por sí solo no demuestra que OpenAI la acepte. Se niega a
actualizar enlaces o un archivo con permisos de grupo u otros usuarios.

Crea una copia privada de la configuración anterior y sustituye únicamente la
línea `AI_API_KEY=` de `.env.hostinger`, conservando exactamente todas las otras
líneas, contraseña y clave de cifrado. La escritura es atómica, el archivo nuevo
tiene permisos `600` y se comprueba que nadie haya editado el archivo mientras
se introducía el valor. Las copias contienen secretos: consérvalas privadas y
fuera de Git. No vuelve a ejecutar el asistente de instalación inicial, no
genera una clave de cifrado nueva, no modifica la base de datos ni llama a APIs.

Cuando termine correctamente, aplica la variable al servicio y comprueba sin
generar texto:

```sh
docker compose -f compose.hostinger.yaml up -d --no-deps --no-build --force-recreate --wait --wait-timeout 120 agent
docker compose -f compose.hostinger.yaml exec -T agent python - < scripts/check_openai.py
```

Recrear solo `agent` interrumpe brevemente esa aplicación y mantiene el volumen
existente. No detiene n8n ni otros proyectos. Un simple `restart` no carga cambios
del archivo de variables. Comparte únicamente la salida del diagnóstico.

## 5. Prueba piloto y activación gradual

Antes de usar documentos privados:

- Comprueba salud, rechazo del acceso privado sin login y navegación en Safari
  del iPad. Una prueba local en Chromium no sustituye esa comprobación.
- Crea historial de prueba y verifica que persiste al recrear **solo este
  servicio**, conservando el volumen y la clave. Configura una copia consistente
  de SQLite con su API de backup o con el servicio detenido; copiar solo el
  archivo mientras está activo puede omitir el WAL. Guarda la clave aparte y
  prueba la restauración en un proyecto aislado, con envíos y APIs desactivados.
- Autoriza por separado un cupo de llamadas reales a OpenAI y prueba la lectura
  de documentos ficticios. No se han autorizado llamadas adicionales en este
  despliegue ni se promete un costo fijo por documento.
- Configura Gmail OAuth con el callback
  `https://agente.lilyapp.mx/api/billing/gmail/callback`, conecta tu buzón y
  revisa un borrador. Para uso continuado resuelve el estado de pruebas de Google
  y su posible caducidad de siete días; una app pública puede necesitar revisión.
- Activa `BILLING_LIVE_EMAIL_ENABLED=true` únicamente cuando autorices la prueba
  de envío a un buzón controlado. La interfaz seguirá pidiendo aprobar cada
  correo. Comprueba remitente, destino, adjuntos, respuesta y duplicados antes de
  usar comercios reales.
- Telegram es el canal principal elegido para el flujo fiscal. Configura un bot, secreto y lista de usuarios; registra
  su webhook con autorización. Las consultas SAT y el seguimiento periódico son
  activaciones separadas con consentimiento, descritas en [FACTURACION.md](FACTURACION.md).

Telegram ahora puede conversar con el mismo agente de IA y conserva el flujo
fiscal con botones. Consulta [TELEGRAM.md](TELEGRAM.md) para la conexión inicial,
la verificación de la primera cuenta y la administración manual de invitados.
La actualización requiere reconstruir la imagen del backend y recrear solo
`agent`; no basta con `git pull`. WhatsApp no está integrado todavía.

Tras cambiar `.env.hostinger`, recrea solo el servicio con el comando `up` del
paso 3. No genera ni cambia la clave de cifrado. El historial general y el módulo
fiscal tienen el alcance de protección descrito en el README y la guía fiscal.
Esta versión sigue siendo una instancia privada de una réplica; no está
preparada para acceso de clientes públicos o múltiples réplicas.

## Prueba local de esta configuración, sin usar el VPS

Después de construir la imagen local como se indica en el README:

```sh
uv run --frozen python scripts/hostinger_smoke.py
```

El script usa el Compose real con un proyecto y un puerto local temporales,
contraseña y documentos ficticios. Verifica permisos del archivo, rechazo de
sobrescritura, caracteres especiales en la contraseña, cookies seguras,
restricción de host, volumen persistente y el documento cifrado después de
recrear el contenedor. No modifica CyberPanel, el VPS ni APIs externas. Elimina
exclusivamente sus contenedores y volumen de prueba.

Para probar el proxy con OpenLiteSpeed real en una máquina de desarrollo:

```sh
docker pull litespeedtech/openlitespeed:latest
uv run --frozen python scripts/cyberpanel_proxy_smoke.py
```

Requiere la imagen local del backend y `openssl`. Crea un certificado ficticio
que solo confía el cliente de esa prueba, sin desactivar TLS ni usar claves
reales. Verifica Host/Origin, login, cookies, redirección HTTP, challenge ACME,
privacidad del accesslog y no-store. No instala esa imagen de OpenLiteSpeed en el
VPS: en producción se aprovecha el servidor ya administrado por CyberPanel.

La misma prueba genera definiciones antiguas ficticias, incluidas duplicadas y
un archivo con sintaxis inválida. Prepara la propuesta de retirada, conserva ese
archivo, reutiliza el certificado de respaldo y verifica el servidor con esa
propuesta, incluidos ambos listeners HTTPS. Las pruebas de Python comprueban
además la copia privada y la restauración del archivo global cuando falla su
validación. No usan datos ni dominios antiguos reales del usuario.
