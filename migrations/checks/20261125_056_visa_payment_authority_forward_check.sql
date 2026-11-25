SELECT
(SELECT count(*) FROM sqlite_master WHERE type='table' AND name='revenue_fee_item_visa_links')=1
AND (SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name IN
('revenue_fee_item_visa_links_validate','revenue_fee_item_visa_links_no_update','revenue_fee_item_visa_links_no_delete','revenue_client_payment_allocations_validate','revenue_client_payment_allocations_no_overallocation','revenue_client_payment_allocations_no_update','revenue_client_payment_allocations_no_delete'))=7;
