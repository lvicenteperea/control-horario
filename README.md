# Control horario

Importador Python 3.11+ de partes de horas Excel a MariaDB 11.5. Cada pestaña representa una persona y contiene `Fecha`, `Horas` y `Descripcicón` (se leen las tres primeras columnas). El archivo original se abre en modo lectura y no se modifica.

## Primera ejecución en Windows (PowerShell)

```powershell
git clone https://github.com/lvicenteperea/control-horario.git
cd control-horario
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.ini config.local.ini
```

La configuración de ejemplo apunta a `localhost:13306`, usuario `root`, base `pruebas`. No incluye contraseña: cada comando que conecta la solicita sin mostrarla. Introduce la contraseña correcta de tu servidor. No se prueban contraseñas alternativas automáticamente. También se puede usar la variable de entorno `CONTROL_HORARIO_DB_PASSWORD`.

La conexión se ejecuta en tu Windows, donde `localhost` es tu ordenador. Debe existir la base `pruebas`; si no existe, crearla desde DBeaver:

```sql
CREATE DATABASE IF NOT EXISTS pruebas CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

Comprobar primero el Excel, sin conectar ni escribir en la base:

```powershell
.\.venv\Scripts\python.exe control_horario.py preview "C:\ruta\Horas CORE.xlsx"
```

Después comprobar conexión, crear los objetos y cargar los datos:

```powershell
.\.venv\Scripts\python.exe control_horario.py check-db
.\.venv\Scripts\python.exe control_horario.py init-db
.\.venv\Scripts\python.exe control_horario.py import "C:\ruta\Horas CORE.xlsx" --source horas-core
```

`init-db` crea únicamente tablas y una vista con prefijo `ch_`. No borra ni modifica tablas de CORE, DATA u otras aplicaciones. Requiere permisos para crear tablas/vistas en la base seleccionada. Las futuras migraciones deberán realizarse explícitamente; `IF NOT EXISTS` no actualiza esquemas existentes.

## Añadir meses nuevos (carga incremental)

`control_horario.py` se mantiene para **cargas completas**. Para añadir meses nuevos, usar **`cargar_nuevos.py`**. Reutiliza la misma configuración y las reglas guardadas. Conserva los registros y las asignaciones ya existentes.

Si ya se instaló la versión inicial, actualizar el repositorio y preparar una vez la nueva tabla y la vista. No borra los datos existentes:

```powershell
git pull --ff-only
.\.venv\Scripts\python.exe control_horario.py init-db
```

Después, cada mes:

```powershell
.\.venv\Scripts\python.exe cargar_nuevos.py "C:\ruta\Horas noviembre.xlsx"
```

Se puede pasar un Excel con solo el mes nuevo o el histórico completo con meses añadidos. Se comparan los registros por **persona y año-mes**, incluyendo la fecha, horas, descripción y número de ocurrencias de cada parte:

- Un mes sin registros previos para esa persona se añade. Puede ser un mes antiguo que faltaba o el mes de una persona todavía no cargada.
- Un mes existente con exactamente los mismos partes se omite, aunque cambien las filas o su orden. Repetir el archivo no duplica las horas ni crea otra importación si no hay novedades.
- Si un mes existente difiere o aparece solo una parte de sus registros, se rechaza **todo el archivo** sin añadir nada. Este programa no modifica ni completa un mes ya cargado: enviar meses completos por persona. Las correcciones se tratarán con un proceso de modificación; también es posible una carga completa revisada.

El origen debe tener una primera carga completa hecha con `control_horario.py import`. Mantener el mismo `--source` (por defecto `horas-core`) y los nombres de las personas. Si el fichero mensual comienza con una fecha vacía, debe rellenarse: no se heredan fechas de otro archivo ni de la BBDD.

Para ver qué añadiría, comparándolo con la base sin guardar cambios:

```powershell
.\.venv\Scripts\python.exe cargar_nuevos.py "C:\ruta\Horas noviembre.xlsx" --dry-run
```

Esta previsualización requiere conexión a MariaDB. La tabla nueva `ch_incremental_imports` vincula cada ampliación con la carga completa activa. Los informes de `ch_v_hours` incluyen la carga completa y sus ampliaciones. Una carga completa posterior reemplaza todo ese conjunto activo, conservando el historial.

## Sustituir el conjunto con una carga completa

Añadir datos o correcciones al Excel y repetir `import` con **el archivo completo**, todas las personas y el mismo `--source horas-core`. El proceso guarda una nueva instantánea y cambia la importación activa solo después de comprobar que cuadran las horas. Las anteriores permanecen en el historial. Reimportar el mismo archivo no aumenta las horas de los informes activos.

Una carga parcial sustituiría la vista activa por ese archivo parcial: no usar un fichero solo del último mes bajo el mismo origen. El nombre `--source` identifica un conjunto independiente, no un mes. No utilizar orígenes distintos para versiones del mismo Excel, porque los informes sumarían ambos. Una fila eliminada desaparece de los informes activos, pero permanece en el historial anterior.

Los registros idénticos de diferentes filas se conservan: pueden ser partes legítimos. En esta versión el ID de registro pertenece a cada instantánea; no es estable entre cargas. Las reglas revisadas se conservan por descripción normalizada (mayúsculas y espacios). No se deduplican ni actualizan registros por número de fila. Cuando se incorporen correcciones manuales por registro en la web, será necesario añadir un ID estable de origen. No editar directamente las asignaciones generadas esperando que sobrevivan a una reimportación: utilizar reglas.

## Fechas, horas y clasificación

- Una fecha vacía hereda la última fecha válida de esa pestaña, incluso al atravesar un subtotal o cabecera repetida. Se guarda `date_inherited`. Sin fecha anterior, la importación se rechaza.
- Se excluyen cabeceras `Fecha/Horas`, filas vacías y totales cuyo primer valor es un nombre de mes en español.
- Fechas Excel reales o texto `dd/mm/aaaa`, `aaaa-mm-dd`, `dd-mm-aaaa`. Horas decimales, incluida coma decimal, no negativas y con hasta seis decimales. Las fórmulas de fecha/horas se rechazan: sustituirlas por valores calculados antes de importar.
- Sin descripción: `[SIN TAREA]`. Texto libre: `[PENDIENTE DE CLASIFICAR]`.
- Los bloques `[ÁMBITO] descripción` generan propuestas de tareas. El ámbito no es un código único de tarea. Se admiten bloques separados por saltos de línea, comas, barras y otros textos. Un prefijo libre no se descarta: genera una asignación pendiente adicional.
- Las tareas propuestas están **pendientes de revisión**. Un solo bloque puede contener varias actividades; no se divide arbitrariamente por cada coma o «y». No se realiza todavía clasificación semántica con IA. Las descripciones largas o bloques incompletos quedan pendientes.
- Varias tareas distintas se reparten por igual. Menciones repetidas de la misma tarea se unifican. Se guardan seis decimales y el residuo se asigna a la última tarea, de forma que el total coincide exactamente. El reparto es una estimación.
- Cualquier fila inválida detiene la lectura completa sin cargar datos. Una excepción durante la carga revierte la transacción; la importación activa anterior sigue vigente.

## Revisar y reutilizar clasificaciones

Crear un JSON local siguiendo `rules.example.json`: una descripción exacta y su lista de tareas. Los ejemplos son ilustrativos, no reglas ya aprobadas. Las reglas reutilizan tareas por ámbito/nombre normalizados y permiten agrupar distintas redacciones. Se aprueban conscientemente con:

```powershell
.\.venv\Scripts\python.exe control_horario.py preview "C:\ruta\Horas CORE.xlsx" --rules "C:\datos\reglas.json"
.\.venv\Scripts\python.exe control_horario.py rules "C:\datos\reglas.json"
.\.venv\Scripts\python.exe control_horario.py import "C:\ruta\Horas CORE.xlsx" --source horas-core
```

Guardar reglas no modifica las cargas anteriores. Reimportar para aplicarlas. Las reglas nuevas actualizan la misma descripción y conservan las demás reglas de la base. Por ahora el reparto de una regla es siempre igualitario. No se admiten aún porcentajes manuales ni excepciones por registro. No subir JSON con descripciones internas al repositorio público.

## Consultas para DBeaver y futura web

Consultar `ch_v_hours`, que reúne las asignaciones de la carga completa activa y sus ampliaciones de cada origen. `sql/queries.sql` incluye ejemplos por mes, persona y tarea, pendientes y reconciliación. Sumar `hours`; `original_hours` se repite por tarea y no debe sumarse en esta vista.

Tablas: `ch_sources`, `ch_imports`, `ch_records`, `ch_tasks`, `ch_allocations`, `ch_rules`, `ch_incremental_imports`. El importador no crea todavía una web ni una API. Son el siguiente paso, con esta vista como base.

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Las pruebas de parser y reparto se ejecutan sin BBDD. La prueba de integración requiere una base **aislada** `control_horario_test` y las variables `CH_TEST_DB=1`, `CH_TEST_PASSWORD` y opcionalmente `CH_TEST_PORT`. Crea datos de prueba y no debe apuntarse a producción. GitHub Actions ejecuta también esta prueba con un servicio MariaDB 11.5 efímero.

El repositorio público contiene solo código, configuración de ejemplo y ejemplos ficticios. `config.local.ini`, los Excel y los CSV están excluidos de Git. Mantener también fuera los datos de empleados y contraseñas. Los resultados reales de la previsualización no se guardan en el repositorio.
