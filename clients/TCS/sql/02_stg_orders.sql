-- 02_stg_orders.sql -> view stg_orders
--
-- Typed/filtered Shopify orders from the direct-API mirror raw_windsor.tcs_shopify_orders.
-- Keyed on the buyer email (contact_email, falling back to customer_email) -- the join key
-- the quiz-conversion attribution uses. Line-item titles are flattened to a display string.
CREATE OR REPLACE VIEW `agora-data-driven.client_tcs.stg_orders` AS
SELECT
  LOWER(TRIM(COALESCE(contact_email, customer_email))) AS email,
  name                            AS order_name,
  created_at                      AS order_date,
  CAST(subtotal_price AS FLOAT64) AS subtotal_price,
  CAST(total_price    AS FLOAT64) AS total_price,
  primary_discount_code,
  ARRAY_TO_STRING(
    ARRAY(SELECT li.title FROM UNNEST(line_items) AS li WHERE li.title IS NOT NULL),
    ', '
  )                               AS products
FROM `agora-data-driven.raw_windsor.tcs_shopify_orders`
WHERE COALESCE(contact_email, customer_email) IS NOT NULL;
