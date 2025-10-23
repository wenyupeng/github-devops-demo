# terraform/production/container-registry.tf

resource "azurerm_container_registry" "acr" {
  name                = "${var.prefix}acr2025"
  resource_group_name = azurerm_resource_group.my_resource_group.name
  location            = var.location
  sku                 = "Basic"
  admin_enabled       = true
}
