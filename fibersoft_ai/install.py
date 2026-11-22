import frappe

DEFAULT_CONTEXT = """Company: (your company name)
Software: Fibersoft ERP.
Industry: (e.g. Apparel & Garments / Mobile Phones & Laptops / Import Export & Electronics / Packaging & Pharma / Manufacturing)
Currency: PKR. Fiscal year: July - June.
Conventions:
- (e.g. "Dealer" means a Customer in customer group Dealers)
- (e.g. COD parcels are Sales Invoices with a courier and CN number)
Important custom reports: (list report names and what they show)"""


def after_install():
	s = frappe.get_single("Fibersoft AI Settings")
	if not s.business_context:
		s.business_context = DEFAULT_CONTEXT
	s.flags.ignore_mandatory = True
	s.save(ignore_permissions=True)
