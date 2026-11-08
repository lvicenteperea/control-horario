-- Ejecutar dentro de la BBDD elegida (pruebas por defecto).
-- Solo crea objetos con prefijo ch_; no modifica tablas de otras aplicaciones.
CREATE TABLE IF NOT EXISTS ch_sources (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 name VARCHAR(190) NOT NULL UNIQUE,
 active_import_id BIGINT UNSIGNED NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ch_imports (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 source_id BIGINT UNSIGNED NOT NULL,
 filename VARCHAR(255) NOT NULL,
 file_sha256 CHAR(64) NOT NULL,
 created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 record_count INT UNSIGNED NOT NULL,
 total_hours DECIMAL(16,6) NOT NULL,
 FOREIGN KEY (source_id) REFERENCES ch_sources(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ch_records (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 import_id BIGINT UNSIGNED NOT NULL,
 person VARCHAR(190) NOT NULL,
 sheet_name VARCHAR(190) NOT NULL,
 source_row INT UNSIGNED NOT NULL,
 work_date DATE NOT NULL,
 date_inherited BOOLEAN NOT NULL,
 original_hours DECIMAL(12,6) NOT NULL CHECK (original_hours >= 0),
 original_description LONGTEXT NOT NULL,
 description_hash CHAR(64) NOT NULL,
 FOREIGN KEY (import_id) REFERENCES ch_imports(id),
 UNIQUE KEY uq_ch_origin (import_id, sheet_name, source_row),
 INDEX ix_ch_date_person (work_date, person)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ch_tasks (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 task_key CHAR(64) NOT NULL UNIQUE,
 scope VARCHAR(190) NOT NULL,
 name VARCHAR(500) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ch_allocations (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 record_id BIGINT UNSIGNED NOT NULL,
 task_id BIGINT UNSIGNED NOT NULL,
 hours DECIMAL(12,6) NOT NULL CHECK (hours >= 0),
 method VARCHAR(30) NOT NULL,
 review_status VARCHAR(30) NOT NULL,
 FOREIGN KEY (record_id) REFERENCES ch_records(id),
 FOREIGN KEY (task_id) REFERENCES ch_tasks(id),
 UNIQUE KEY uq_ch_record_task (record_id, task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ch_rules (
 description_hash CHAR(64) NOT NULL PRIMARY KEY,
 original_description LONGTEXT NOT NULL,
 tasks_json JSON NOT NULL,
 updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
  ON UPDATE CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Las cargas incrementales pertenecen a la carga completa que amplían.
-- Una carga completa posterior reemplaza la vista activa de todo el conjunto.
CREATE TABLE IF NOT EXISTS ch_incremental_imports (
 import_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
 base_import_id BIGINT UNSIGNED NOT NULL,
 FOREIGN KEY (import_id) REFERENCES ch_imports(id),
 FOREIGN KEY (base_import_id) REFERENCES ch_imports(id),
 INDEX ix_ch_incremental_base (base_import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE OR REPLACE VIEW ch_v_hours AS
 SELECT s.name AS source, i.id AS import_id, r.id AS record_id,
 r.person, r.work_date, DATE_FORMAT(r.work_date, '%Y-%m') AS month,
 t.id AS task_id, t.scope, t.name AS task, a.hours,
 a.method, a.review_status, r.original_hours, r.original_description,
 r.date_inherited, r.sheet_name, r.source_row
 FROM ch_sources s
 JOIN ch_imports i ON i.source_id=s.id
  AND (i.id=s.active_import_id OR EXISTS (
   SELECT 1 FROM ch_incremental_imports ci
   WHERE ci.import_id=i.id AND ci.base_import_id=s.active_import_id
  ))
 JOIN ch_records r ON r.import_id=i.id
 JOIN ch_allocations a ON a.record_id=r.id
 JOIN ch_tasks t ON t.id=a.task_id;
