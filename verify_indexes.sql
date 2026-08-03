-- ==============================================================================
-- HNSW & Vector Index Verification Script
-- Run after schema.sql to verify all indexes are healthy
-- ==============================================================================

-- 1. Check pgvector extension version
SELECT 
    extname,
    extversion,
    extnamespace::regnamespace AS schema
FROM pg_extension 
WHERE extname = 'vector';

-- 2. Verify HNSW index exists and is valid
SELECT 
    schemaname,
    tablename,
    indexname,
    indexdef
FROM pg_indexes 
WHERE indexname = 'idx_products_embedding_hnsw';

-- 3. Check all indexes on products table
SELECT 
    indexname,
    indexdef
FROM pg_indexes 
WHERE tablename = 'products'
ORDER BY indexname;

-- 4. Verify partition setup for price_history
SELECT 
    parent.relname AS parent_table,
    child.relname AS partition_name,
    pg_get_expr(child.relpartbound, child.oid) AS partition_range
FROM pg_inherits
JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
JOIN pg_class child ON pg_inherits.inhrelid = child.oid
WHERE parent.relname = 'price_history'
ORDER BY child.relname;

-- 5. Verify partition setup for affiliate_clicks
SELECT 
    parent.relname AS parent_table,
    child.relname AS partition_name,
    pg_get_expr(child.relpartbound, child.oid) AS partition_range
FROM pg_inherits
JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
JOIN pg_class child ON pg_inherits.inhrelid = child.oid
WHERE parent.relname = 'affiliate_clicks'
ORDER BY child.relname;

-- 6. Test HNSW index with a sample query (requires data)
-- Uncomment after inserting products with embeddings:
-- EXPLAIN ANALYZE
-- SELECT id, title, 1 - (title_embedding <=> '[0.1, 0.2, ... 384 dims]') AS similarity
-- FROM products
-- ORDER BY title_embedding <=> '[0.1, 0.2, ... 384 dims]'
-- LIMIT 10;

-- 7. Check index sizes
SELECT 
    schemaname,
    tablename,
    indexname,
    pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM pg_indexes
JOIN pg_class ON pg_indexes.indexname = pg_class.relname
WHERE schemaname = 'public'
ORDER BY pg_relation_size(indexrelid) DESC
LIMIT 20;

-- 8. Verify BRIN indexes on partitions
SELECT 
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE indexname LIKE 'price_history_%_brin'
   OR indexname LIKE 'affiliate_clicks_%_brin'
ORDER BY tablename;

-- 9. Check materialized view exists
SELECT 
    schemaname,
    matviewname,
    hasindexes,
    ispopulated
FROM pg_matviews
WHERE matviewname = 'mv_best_prices';

-- 10. Verify custom types
SELECT 
    typname,
    typtype,
    typcategory
FROM pg_type
WHERE typname IN (
    'affiliate_status',
    'notification_status', 
    'notification_priority',
    'wallet_txn_type',
    'verification_method',
    'claim_status',
    'offer_source',
    'ocr_status'
)
ORDER BY typname;
