<?xml version="1.0" encoding="UTF-8"?>
<!--
	Display preferences for LAAD3 (Dictionary > Settings > LAAD3).
	Only adds classes / the "open" attribute; all visual rules live in the stylesheet.
-->
<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
				xmlns:d="http://www.apple.com/DTDs/DictionaryService-1.0.rng"
				version="1.0">
<xsl:output method="xml" encoding="UTF-8" indent="no"
	doctype-public="-//W3C//DTD XHTML 1.1//EN"
	doctype-system="http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd" />

<xsl:param name="display-expand" select="'0'" />
<xsl:param name="display-exaaudio" select="'1'" />
<xsl:param name="display-pictures" select="'1'" />

<xsl:template match="d:entry">
	<xsl:copy>
		<xsl:apply-templates select="@*" />
		<xsl:attribute name="class">
			<xsl:text>laad</xsl:text>
			<xsl:if test="$display-exaaudio = '0'"> no-exa-audio</xsl:if>
			<xsl:if test="$display-pictures = '0'"> no-pictures</xsl:if>
		</xsl:attribute>
		<xsl:apply-templates select="node()" />
	</xsl:copy>
</xsl:template>

<!-- collapsible thesaurus / collocation items -->
<xsl:template match="*[local-name()='details' and contains(concat(' ', normalize-space(@class), ' '), ' x ')]">
	<xsl:copy>
		<xsl:apply-templates select="@*" />
		<xsl:if test="$display-expand = '1'">
			<xsl:attribute name="open">open</xsl:attribute>
		</xsl:if>
		<xsl:apply-templates select="node()" />
	</xsl:copy>
</xsl:template>

<xsl:template match="@*|node()">
	<xsl:copy>
		<xsl:apply-templates select="@*|node()" />
	</xsl:copy>
</xsl:template>

</xsl:stylesheet>
