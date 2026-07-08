-- 04_quiz_conversion.sql -> view quiz_conversion
--
-- Per lead (email), attribute Shopify orders placed AT/AFTER the quiz (with the same 5-minute
-- buffer as the old "$33k" logic, to catch the checkout race where the order lands a moment
-- before the quiz confirmation). Because stg_quiz is already one row per email, each order maps
-- cleanly to its lead -- no closest-quiz dedup needed. order_seq=1 is the FIRST order.
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.quiz_conversion` AS
WITH matched AS (
  SELECT
    q.email,
    o.order_name, o.order_date, o.subtotal_price, o.primary_discount_code, o.products,
    ROW_NUMBER() OVER (PARTITION BY q.email ORDER BY o.order_date) AS order_seq
  FROM `agora-data-driven.client_tcs.stg_quiz`   AS q
  JOIN `agora-data-driven.client_tcs.stg_orders` AS o
    ON o.email = q.email
   AND o.order_date >= TIMESTAMP_SUB(q.submitted_at, INTERVAL 5 MINUTE)
)
SELECT
  email,
  TRUE                                                AS is_converted,
  MIN(order_date)                                     AS first_order_date,
  COUNT(DISTINCT order_name)                          AS order_count_post_quiz,
  SUM(subtotal_price)                                 AS revenue_post_quiz,
  MAX(IF(order_seq = 1, order_name, NULL))            AS first_order_name,
  MAX(IF(order_seq = 1, products, NULL))              AS first_order_products,
  MAX(IF(order_seq = 1, primary_discount_code, NULL)) AS first_order_discount_code
FROM matched
GROUP BY email;
