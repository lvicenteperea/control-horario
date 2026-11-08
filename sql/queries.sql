-- Las horas de las tareas se suman con hours; original_hours se repite
-- por asignación y NO debe sumarse desde esta vista.
SELECT month, person, SUM(hours) AS hours
FROM ch_v_hours GROUP BY month, person ORDER BY month, person;

SELECT scope, task, SUM(hours) AS hours
FROM ch_v_hours GROUP BY scope, task ORDER BY hours DESC;

SELECT person, work_date, original_description, hours, task, source_row
FROM ch_v_hours WHERE review_status='pendiente'
ORDER BY work_date, person;

-- Reconciliación independiente: no debe devolver filas.
SELECT r.id, r.original_hours, COALESCE(SUM(a.hours),0) AS allocated
FROM ch_records r
LEFT JOIN ch_allocations a ON a.record_id=r.id
GROUP BY r.id, r.original_hours
HAVING r.original_hours <> COALESCE(SUM(a.hours),0);
