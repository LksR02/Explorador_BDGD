SELECT
  COUNT(*) AS uc_bt,
  COUNT(DISTINCT u.UNI_TR_MT) AS transformadores_referenciados,
  SUM(CASE WHEN t.COD_ID IS NULL THEN 1 ELSE 0 END) AS uc_com_transformador_inexistente,
  SUM(CASE WHEN f.COD_ID IS NULL THEN 1 ELSE 0 END) AS uc_com_alimentador_inexistente
FROM UCBT_tab u
LEFT JOIN UNTRMT t ON t.COD_ID = u.UNI_TR_MT
LEFT JOIN CTMT f ON f.COD_ID = u.CTMT
