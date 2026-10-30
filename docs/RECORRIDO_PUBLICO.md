# Recorrido público del ticket

La versión actual se describe y valida en [Sesión 3A: portal correcto](PORTAL_CORRECTO.md). La revisión llega hasta los campos del formulario.

La extracción conserva las direcciones impresas con su propia frase. Elige la indicada para obtener el comprobante y registra por qué descarta las demás. Antes de anunciarla comprueba que resuelva y responda, usando DNS público, conexión a IP fijada y TLS. Esa comprobación no observa campos ni sigue redirecciones.

Si no hay una dirección válida, busca primero una memoria propia del comercio y RFC emisor, después la web oficial y su acceso de facturación. El descubrimiento con `web_search` exige fuentes y evidencia. Si no puede verificarlo, pide una dirección escrita. El correo expresamente identificado para facturar y la asistencia manual conservan sus flujos existentes.

El agente decide con la página actual. Recibe texto y contexto, enlaces, botones, descripciones de imágenes, pestañas, menús y opciones observadas. Puede interpretar otros idiomas y etiquetas de campos desconocidas. Las referencias corresponden a la observación vigente; nunca admite URLs, selectores o JavaScript inventados por el modelo. El contenido de la página sigue siendo dato no confiable.

El permiso inicial habilita el recorrido dentro de sus presupuestos. Cada nuevo dominio documental —incluidos iframes y redirecciones— requiere un permiso explícito. Los recursos auxiliares admitidos heredan la autorización del recorrido dentro del límite de 12 dominios; no conceden permiso para observar documentos de esos dominios. Se conservan DNS público, IP fijada, TLS, bloqueo de rastreo y solo GET. Imágenes, fuentes y medios mantienen su omisión previa. Telegram muestra la cadena de dominios antes de acceder al destino. Los permisos siguen vinculados al propietario y al ticket, con caducidad y uso único. Una memoria de portal no concede autorización.

La revisión termina al reconocer y guardar un formulario público utilizable. Lee los campos que pide; solicita al usuario los datos ausentes. Un callejón sin salida muestra dónde se detuvo y ofrece escribir la dirección. No abre automáticamente trabajos sucesivos para seguir reintentando.

Se mantienen ocho pasos contando la apertura, 12 dominios, 60 solicitudes por página, 240 por recorrido, 4 MiB por respuesta, 8 MiB por página y 24 MiB por recorrido. El avance automático tiene 22 segundos para iniciar pasos. Siguen vigentes los límites del Runner, de herramientas, de tokens y las cuotas.

Los POST auxiliares siguen bloqueados antes de salir a la red; las operaciones denegadas cuentan para sus límites. La revisión no habilita POST principales, WebSockets, descargas, redes privadas ni saltarse CAPTCHA o login. El [selector de marcas](SELECTOR_MARCAS.md) describe la separación de esas peticiones auxiliares.

Las pruebas de esta sesión usan páginas y transportes simulados con Chromium y SDK reales. El informe registra sus resultados y las limitaciones; no demuestra compatibilidad universal ni constituye un despliegue en el VPS.
